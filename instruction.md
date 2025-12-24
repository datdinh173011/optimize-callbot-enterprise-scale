# Optimize Callbot Enterprise Scale (Senior Level)

## Objective

Diagnose and resolve **critical performance issues** in a **Django-based Callbot AI system** serving Enterprise customers. The system is experiencing **504 Gateway Timeout** errors when large Enterprise workspaces access their data through the Customer API.

Your task is to perform **systematic performance profiling**, identify bottlenecks, and implement optimizations to achieve **sub-500ms response times** for the first page of results in workspaces with up to 1 million customers.

## Background Context

### Data Scale
| Table | Total Records | Description |
|-------|--------------|-------------|
| `Customer` | 70,000,000 | Customer profiles across all workspaces |
| `Call` | 1,000,000,000 | Call history records |
| Database Size | ~2TB | PostgreSQL |

### Multi-tenant Architecture
- Data is partitioned by `workspace_id`
- Largest Enterprise workspace contains:
  - **1,000,000 Customers**
  - **50,000,000 Calls**

### Current Symptoms
- `GET /api/v1/customers/?workspace_id=X` returns **504 Gateway Timeout** for large workspaces
- Response time degrades exponentially as page number increases
- Memory usage spikes during serialization

---

## Service Specifications

### Database Service (PostgreSQL)
| Parameter | Value |
|-----------|-------|
| Version | PostgreSQL 15+ |
| Max Connections | 200 |
| Shared Buffers | 4GB |
| Work Memory | 256MB |
| Maintenance Work Memory | 1GB |
| Effective Cache Size | 12GB |
| Random Page Cost | 1.1 (SSD) |
| Max Parallel Workers | 4 |

### Application Service (Django/DRF)
| Parameter | Value |
|-----------|-------|
| Python Version | 3.11+ |
| Django Version | 4.2+ |
| DRF Version | 3.14+ |
| Gunicorn Workers | 4 |
| Worker Timeout | 30s |
| Request Timeout | 60s (Nginx) |
| Max Request Body | 10MB |

### Cache Service (Redis)
| Parameter | Value |
|-----------|-------|
| Version | Redis 7+ |
| Max Memory | 2GB |
| Eviction Policy | allkeys-lru |
| Connection Pool | 50 |

---

## Data Models & Response Types

### Current Index Sizes (Production)
| Schema | Table | Index Size | Notes |
|--------|-------|------------|-------|
| public | `Call` | **129 GB** | Multiple single-column indexes |
| public | `Customer` | **66 GB** | Includes redundant indexes |
| **Total** | - | **195 GB** | ⚠️ Exceeds available RAM |

> **Problem**: Index size exceeds available RAM (12GB effective cache), causing frequent disk reads.

### Customer Model

```python
class Customer(SoftDeleteModel):
    """
    70M records in production!
    Inherits SoftDeleteModel - all queries include is_deleted=FALSE
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    workspace_id = models.UUIDField(db_index=True)  # ⚠️ Single column - not enough!
    
    # Basic info
    name = models.CharField(max_length=255)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20)
    carrier = models.CharField(max_length=50, null=True)  # Phone carrier
    
    # Status fields (heavily filtered!)
    status = models.CharField(max_length=20)      # active, inactive, blocked, pending
    quality = models.CharField(max_length=20)     # hot, warm, cold, dead
    call_status = models.CharField(max_length=20) # pending, calling, completed, failed
    
    # Duration tracking
    duration = models.FloatField(default=0.0)     # Total call duration
    
    # ManyToMany labels (causes DISTINCT!)
    campaign_labels = models.ManyToManyField(Label, related_name='customers')
    
    # Flags
    is_test = models.BooleanField(default=False)
    checked = models.BooleanField(default=False)
    
    # Flexible JSON field for custom attributes
    other_attributes = models.JSONField(default=dict)  # ⚠️ SLOW without GIN index!
    
    # Soft delete (inherited from SoftDeleteModel)
    is_deleted = models.BooleanField(default=False)  # ⚠️ ALL queries filter this!
    
    # Employee assignment (affects permission checks!)
    employee = models.ForeignKey(Employee, null=True, db_index=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'customer'
        indexes = [
            # ⚠️ MISSING: Partial indexes with WHERE is_deleted=FALSE
        ]
```

> **⚠️ SoftDeleteModel Pattern**: Every query automatically filters `is_deleted=FALSE`.
> This means ALL indexes MUST be partial indexes to be effective!

### Call Model
```python
class Call(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    workspace_id = models.UUIDField(db_index=True)
    
    # Customer relationship
    customer_id = models.UUIDField(db_index=True)  # FK to Customer
    
    # Campaign relationship (for billing/reporting)
    campaign_customer = models.ForeignKey(
        "campaign_call.CallCampaignCustomer",
        related_name="calls",
        on_delete=models.SET_NULL,
        default=None, null=True, blank=True,
        db_index=True
    )  # ⚠️ Affects billing reports per campaign
    
    direction = models.CharField(max_length=10)  # "inbound" | "outbound"
    status = models.CharField(max_length=20)  # "completed" | "missed" | "busy"
    
    # Duration tracking
    duration = models.FloatField(default=0.0)  # Actual duration in seconds
    duration_seconds = models.IntegerField(default=0)  # Rounded duration
    
    # Call metadata
    recording_url = models.URLField(null=True, blank=True)
    transcript = models.TextField(null=True, blank=True)
    sentiment_score = models.FloatField(null=True)  # -1.0 to 1.0
    
    # Billing category (different rates per call type)
    call_type = models.CharField(max_length=20)  # "campaign" | "direct" | "callback"
    billing_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'call'
        indexes = [
            # Existing: single column indexes on workspace_id, customer_id
            # ⚠️ Missing: composite index for latest call lookup
        ]
```

### API Response Format (Full)
```python
# GET /api/v1/customers/?workspace_id=X
{
    "results": [
        {
            # Basic fields
            "id": "uuid-string",
            "workspace_id": "uuid-string",
            "name": "string",
            "email": "string | null",
            "phone": "string",
            "status": "active | inactive | blocked",
            "tags": ["string"],
            "other_attributes": {},
            "is_deleted": false,
            "created_at": "ISO8601 datetime",
            "updated_at": "ISO8601 datetime",
            
            # Employee info (FK - potential N+1)
            "employee": {
                "id": "uuid-string",
                "name": "string",
                "email": "string"
            } | null,
            
            # ========================================
            # COMPUTED FIELDS FROM CALL TABLE
            # ⚠️ These cause N+1 queries if not optimized!
            # ========================================
            
            # Latest call info
            "latest_call": {
                "id": "uuid-string",
                "direction": "inbound | outbound",
                "status": "completed | missed | busy",
                "duration": 180.5,
                "call_type": "campaign | direct | callback",
                "sentiment_score": 0.75,
                "created_at": "ISO8601 datetime"
            } | null,
            
            # Call statistics (aggregations)
            "total_calls": 150,
            "total_duration_seconds": 27000,
            "calls_last_30_days": 25,
            "successful_calls": 120,
            "missed_calls": 30,
            
            # Campaign-specific stats
            "campaign_stats": {
                "total_campaign_calls": 80,
                "completed_campaigns": 5,
                "active_campaign": {
                    "id": "uuid-string",
                    "name": "string"
                } | null
            },
            
            # Billing summary
            "billing_summary": {
                "total_cost": 1250.50,
                "cost_this_month": 150.25,
                "billing_breakdown": {
                    "campaign": 800.00,
                    "direct": 350.50,
                    "callback": 100.00
                }
            }
        }
    ],
    "next": "cursor-url | null",
    "previous": "cursor-url | null",
    "count": null  # Disabled for performance
}
```

### Permission Complexity

The `employee` field creates additional query complexity:

```python
# Current permission check (SLOW!)
class WorkspacePermission(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        workspace_id = request.query_params.get('workspace_id')
        
        # Check 1: Is user in workspace?
        if not user.workspaces.filter(id=workspace_id).exists():
            return False
        
        # Check 2: Employee-level permission (⚠️ EXPENSIVE!)
        if user.role == 'employee':
            # This filters the entire customer list!
            return Customer.objects.filter(
                workspace_id=workspace_id,
                employee=user.employee,
                is_deleted=False
            ).exists()  # ⚠️ Without index: full table scan!
        
        return True
```

---

### CustomerFilter - Complex Filtering

The Customer API uses `django-filter` with complex IN/NOT IN queries:

```python
class CustomerFilter(FilterSet):
    """
    ⚠️ Each filter adds WHERE conditions - need proper indexes!
    All queries automatically include: WHERE is_deleted = FALSE
    """
    
    # IN queries (comma-separated values)
    # Example: ?status=active,pending -> WHERE status IN ('active', 'pending')
    status = CharFilter(field_name='status', method="filter_field_in")
    quality = CharFilter(field_name='quality', method="filter_field_in")
    call_status = CharFilter(field_name='call_status', method="filter_field_in")
    
    # NOT IN queries (exclusions)
    # Example: ?status__neq=blocked -> WHERE status NOT IN ('blocked')
    status__neq = CharFilter(field_name='status', method="exclude_field_in")
    quality__neq = CharFilter(field_name='quality', method="exclude_field_in")
    call_status__neq = CharFilter(field_name='call_status', method="exclude_field_in")
    duration__neq = CharFilter(field_name='duration', method="exclude_field")
    
    # ManyToMany labels (causes JOINs + DISTINCT!)
    # Example: ?labels__id=uuid1,uuid2 -> JOIN labels WHERE label_id IN (...)
    labels__id = CharFilter(field_name='campaign_labels', method="filter_field_in")
    labels__id__neq = CharFilter(field_name='campaign_labels', method="exclude_field_in")
    
    # Null checks
    quality__isnull = CharFilter(method="filter_valuable")
    status__isnull = CharFilter(method="filter_valuable")
    
    # JSON field filter (other_attributes)
    # Example: ?other_attributes=city=Hanoi -> WHERE other_attributes->>'city' = 'Hanoi'
    other_attributes = CharFilter(method="filter_json_field")

    class Meta:
        model = Customer
        fields = {
            "carrier": ["in", "exact"],
            "duration": ["exact", "gt", "lt", "gte", "lte"],
            "call_status": ["in", "exact"],
            "campaign_labels__id": ["in"],
            "is_test": ["exact"],
            "checked": ["exact"],
        }
```

### Filter Query Examples

| Filter | Query | SQL Generated |
|--------|-------|---------------|
| Status IN | `?status=active,pending` | `WHERE status IN ('active','pending') AND is_deleted=FALSE` |
| Status NOT IN | `?status__neq=blocked` | `WHERE status NOT IN ('blocked') AND is_deleted=FALSE` |
| Duration Range | `?duration__gte=60&duration__lte=300` | `WHERE duration BETWEEN 60 AND 300 AND is_deleted=FALSE` |
| Labels | `?labels__id=uuid1` | `JOIN ... WHERE label_id='uuid1' AND is_deleted=FALSE` (DISTINCT!) |
| JSON Field | `?other_attributes=city=Hanoi` | `WHERE other_attributes->>'city'='Hanoi' AND is_deleted=FALSE` |
| Combined | `?status=active&quality=hot` | `WHERE status='active' AND quality='hot' AND is_deleted=FALSE` |
        }
```

### Index Requirements for Soft Delete Pattern

> **CRITICAL**: All models inherit `SoftDeleteModel` with `is_deleted=False` default filter.
> Every query includes this condition, so indexes MUST be partial indexes!

```sql
-- ❌ WRONG: Single-column index (ignores is_deleted condition)
CREATE INDEX idx_customer_workspace ON customer(workspace_id);

-- ✅ CORRECT: Partial composite index
CREATE INDEX idx_customer_workspace_active 
    ON customer(workspace_id, created_at DESC)
    WHERE is_deleted = FALSE;
```

| Model | Required Indexes | Notes |
|-------|-----------------|-------|
| `Customer` | `(workspace_id, created_at DESC) WHERE is_deleted=FALSE` | Pagination |
| `Customer` | `(workspace_id, employee_id) WHERE is_deleted=FALSE` | Permission filter |
| `Customer` | `(workspace_id, status) WHERE is_deleted=FALSE` | Status filtering |
| `Customer` | `(workspace_id, quality) WHERE is_deleted=FALSE` | Quality filtering |
| `Customer` | `(workspace_id, call_status) WHERE is_deleted=FALSE` | Call status filter |
| `Customer` | `(workspace_id, duration) WHERE is_deleted=FALSE` | Duration range queries |
| `Customer` | `GIN(other_attributes) WHERE is_deleted=FALSE` | JSON queries |
| `Call` | `(customer_id, created_at DESC) WHERE is_deleted=FALSE` | Latest call lookup |

---

### ⚠️ Write Performance Considerations

> **CRITICAL**: Adding indexes improves READ but slows down WRITE operations!
> You MUST verify that INSERT/UPDATE performance is still acceptable.

**Write Performance Thresholds:**

| Operation | Max Time | Notes |
|-----------|----------|-------|
| Single INSERT | < 100ms | With 9 indexes to maintain |
| Batch INSERT (100 rows) | < 2s | Should use bulk insert |
| Single UPDATE | < 50ms | Index updates on status/quality |
| Batch UPDATE (100 rows) | < 1s | Common for bulk status changes |
| Soft Delete (50 rows) | < 500ms | Updates is_deleted + deleted_at |

**Index Write Amplification:**
- Each INSERT updates ALL 9 indexes
- Each UPDATE on indexed column(s) must update those indexes
- Partial indexes reduce this (only rows with is_deleted=FALSE are indexed)

### 🔒 Deadlock Prevention

Adding multiple indexes can cause deadlocks under concurrent writes:

```
⚠️ Deadlock Scenario:
Transaction A: UPDATE customer SET status='active' WHERE id IN (1,2,3)
Transaction B: UPDATE customer SET status='inactive' WHERE id IN (3,2,1)

If indexes are updated in different order → DEADLOCK!
```

**Prevention Strategies:**

1. **Consistent Lock Order**: Always update in sorted ID order
2. **Short Transactions**: Minimize transaction duration
3. **Use CONCURRENTLY**: Create indexes without locking writes
4. **Avoid HOT Updates**: Be aware of FILLFACTOR for frequent updates

**Verification Tests Required:**
- Concurrent INSERT (10 threads × 20 writes) → < 5% deadlock rate
- Concurrent UPDATE (5 threads updating shared rows) → 0 deadlocks
- Index lock wait time monitoring

## Requirements

### 1. Performance Profiling (`/app/profiling/`)

Create diagnostic tools to identify performance bottlenecks:

#### 1.1 Query Analyzer (`/app/profiling/query_analyzer.py`)
- Capture all SQL queries executed during a request
- Log query count, total time, and slowest queries
- Detect N+1 query patterns automatically
- Output: JSON report with query statistics

```python
# Expected output format
{
    "total_queries": 1502,
    "total_time_ms": 4523,
    "slowest_queries": [
        {
            "sql": "SELECT ... FROM call WHERE customer_id = ...",
            "time_ms": 234,
            "count": 1500  # N+1 detected!
        }
    ],
    "n_plus_one_detected": true
}
```

#### 1.2 Permission Profiler (`/app/profiling/permission_profiler.py`)
- Analyze `WorkspacePermission` class for expensive operations
- Check if `COUNT(*)` or full table scans occur on every request
- Identify redundant permission checks

#### 1.3 Serializer Profiler (`/app/profiling/serializer_profiler.py`)
- Profile `CustomerSerializer` for N+1 patterns
- Detect lazy loading of related objects (e.g., `latest_call`)
- Measure serialization time per record

#### 1.4 Layer-by-Layer Analysis (`/app/profiling/layer_analyzer.py`)

**CRITICAL**: Before any optimization, create a timing breakdown report at each checkpoint:

| Layer | Checkpoint | Measurement |
|-------|------------|-------------|
| **Middleware** | Request received → View called | Authentication, CORS, logging overhead |
| **Permission** | View called → Permission check complete | `WorkspacePermission.has_permission()` time |
| **QuerySet** | Permission passed → Data fetched | SQL execution time (use `EXPLAIN ANALYZE`) |
| **Serializer** | Data fetched → Response serialized | Python object transformation time |

```python
# Expected layer analysis output format
{
    "request_id": "uuid-string",
    "endpoint": "/api/v1/customers/",
    "workspace_id": "enterprise-ws-1",
    "total_time_ms": 5234,
    "breakdown": {
        "middleware_time_ms": 15,
        "permission_time_ms": 1200,  # ⚠️ BOTTLENECK!
        "queryset_time_ms": 3500,    # ⚠️ BOTTLENECK!
        "serializer_time_ms": 519,
    },
    "bottleneck_type": "io",  # "io" (Database) or "cpu" (Python/Serialization)
    "bottleneck_layer": "queryset",
    "recommendations": [
        "Permission check is performing COUNT(*) - use EXISTS instead",
        "N+1 detected in serializer - 1500 queries for latest_call",
        "Missing composite index on (workspace_id, created_at)"
    ]
}
```

**Requirements**:
1. Implement as middleware that can be enabled via `?_profile=true` query param
2. Store profiling results in Redis for analysis (TTL: 1 hour)
3. Identify if bottleneck is **I/O bound** (Database) or **CPU bound** (Python/Serialization)
4. Generate actionable recommendations based on findings

### 2. Database Optimization (`/app/migrations/`)

Create Django migrations to optimize database performance:

#### 2.1 Index Strategy

**GOAL**: Minimize index RAM usage while maximizing query performance for multi-tenant architecture.

##### Index Design Principles
| Principle | Rationale |
|-----------|-----------|
| **Workspace-first composite** | `workspace_id` always first in composite indexes for tenant isolation |
| **Covering indexes** | Include frequently selected columns to avoid table lookups |
| **Partial indexes** | Index only active/recent data to reduce size |
| **Avoid redundant indexes** | Single-column index on `workspace_id` is redundant if composite exists |

##### Recommended Indexes (Priority Order)

```sql
-- 1. CUSTOMER TABLE: Primary query pattern
-- Supports: List customers by workspace, sorted by created_at (pagination)
-- Size estimate: ~2.8GB for 70M records
CREATE INDEX idx_customer_workspace_created 
    ON customer(workspace_id, created_at DESC, id);
-- Note: Include 'id' for stable cursor pagination

-- 2. CALL TABLE: Latest call per customer lookup
-- Supports: Subquery for latest_call annotation
-- Size estimate: ~8GB for 1B records
CREATE INDEX idx_call_customer_created 
    ON call(customer_id, created_at DESC)
    INCLUDE (id, direction, status, duration_seconds);
-- Note: INCLUDE avoids table lookup for serializer fields

-- 3. CALL TABLE: Aggregations per workspace (optional, evaluate need first)
-- Supports: COUNT queries per workspace
-- Size estimate: ~4GB for 1B records
-- ⚠️ ONLY if you need workspace-level call analytics
CREATE INDEX idx_call_workspace_created 
    ON call(workspace_id, created_at DESC)
    WHERE created_at > NOW() - INTERVAL '90 days';  -- Partial index!
```

##### Index Size Estimation

| Index | Table Size | Estimated Index Size | RAM Impact |
|-------|-----------|---------------------|------------|
| `idx_customer_workspace_created` | 70M rows | ~2.8 GB | Moderate |
| `idx_call_customer_created` | 1B rows | ~8 GB | High |
| `idx_call_workspace_created` (partial) | ~250M rows | ~1 GB | Low |
| **Total New Indexes** | - | **~12 GB** | - |

##### Indexes to DROP (Redundant)

```sql
-- Drop single-column indexes that are covered by composites
DROP INDEX IF EXISTS idx_customer_workspace_id;  -- Covered by composite
DROP INDEX IF EXISTS idx_call_workspace_id;      -- Covered by composite
-- Keep idx_call_customer_id only if used for FK constraints
```

##### Django Migration Implementation

```python
# /app/migrations/0002_add_performance_indexes.py
from django.db import migrations

class Migration(migrations.Migration):
    atomic = False  # Allow concurrent index creation
    
    dependencies = [('app', '0001_initial')]
    
    operations = [
        # Customer composite index
        migrations.RunSQL(
            sql='''
                CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_customer_workspace_created 
                ON customer(workspace_id, created_at DESC, id);
            ''',
            reverse_sql='DROP INDEX IF EXISTS idx_customer_workspace_created;'
        ),
        # Call composite index with covering columns
        migrations.RunSQL(
            sql='''
                CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_call_customer_created 
                ON call(customer_id, created_at DESC)
                INCLUDE (id, direction, status, duration_seconds);
            ''',
            reverse_sql='DROP INDEX IF EXISTS idx_call_customer_created;'
        ),
        # Partial index for recent calls only
        migrations.RunSQL(
            sql='''
                CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_call_workspace_recent 
                ON call(workspace_id, created_at DESC)
                WHERE created_at > NOW() - INTERVAL '90 days';
            ''',
            reverse_sql='DROP INDEX IF EXISTS idx_call_workspace_recent;'
        ),
    ]
```

> ⚠️ **IMPORTANT**: Use `CREATE INDEX CONCURRENTLY` to avoid table locks in production!

#### 2.2 Query Optimization (`/app/api/views.py`)
- Eliminate N+1 queries using `Prefetch` with limited QuerySet
- Use `Subquery`/`OuterRef` for efficient related data fetching
- Implement `select_related` and `prefetch_related` correctly

```python
# Example: Efficient latest_call fetching
from django.db.models import OuterRef, Subquery

latest_call_subquery = Call.objects.filter(
    customer_id=OuterRef('pk')
).order_by('-created_at').values('id')[:1]

customers = Customer.objects.filter(
    workspace_id=workspace_id
).annotate(
    latest_call_id=Subquery(latest_call_subquery)
)
```

### 3. Pagination Optimization (`/app/api/pagination.py`)

Implement efficient pagination for large datasets:

#### 3.1 Cursor-based Pagination
- Replace `LimitOffsetPagination` with `CursorPagination`
- Cursor should be based on `created_at` + `id` for consistency
- Must handle both forward and backward navigation

```python
# Expected pagination response
{
    "results": [...],
    "next": "http://api/customers/?cursor=cD0yMDIzLTEyLTI1",
    "previous": null,
    "count": null  # Count disabled for performance
}
```

#### 3.2 Count Optimization
- Disable exact count for large result sets
- Implement estimated count using `EXPLAIN` or statistics tables
- Return `"count": null` or estimated count instead

### 4. Permission Optimization (`/app/api/permissions.py`)

Optimize workspace permission checks:

- Cache permission results per request using `request._cached_permissions`
- Avoid `COUNT(*)` queries; use `EXISTS` instead
- Implement permission prefetching for batch operations

```python
# Bad: COUNT(*) on every request
def has_permission(self, request, view):
    return Customer.objects.filter(workspace_id=ws).count() > 0

# Good: EXISTS check
def has_permission(self, request, view):
    return Customer.objects.filter(workspace_id=ws).exists()
```

### 5. Caching Layer (`/app/api/caching.py`)

Implement strategic caching:

- Cache workspace metadata (customer count, last updated)
- Use Redis for frequently accessed data
- Implement cache invalidation on data changes
- TTL: 5 minutes for metadata, 1 minute for counts

## Specifications

- **Language**: Python 3.11+
- **Framework**: Django 4.2+, Django REST Framework 3.14+
- **Database**: PostgreSQL 15+
- **Cache**: Redis 7+
- **All optimizations must be in `/app/` directory**

## Constraints

- **NO microservices architecture changes**
- **ALL database changes via Django Migrations**
- **NO data loss or corruption allowed**
- **Optimize queries and indexes BEFORE suggesting hardware upgrades**
- **Maintain backward compatibility with existing API contracts**
- **Use only packages**: `django`, `djangorestframework`, `psycopg2`, `redis`, `django-debug-toolbar`

## Performance Targets

| Metric | Before | Target |
|--------|--------|--------|
| First page response time (1M workspace) | 504 Timeout | < 500ms |
| Query count per request | 1500+ | < 10 |
| Memory usage per request | 2GB+ | < 100MB |
| Page 100 response time | N/A | < 1000ms |

## Example Usage

```python
import httpx

# Test optimized endpoint
client = httpx.Client(timeout=30.0)

# First page should be fast
response = client.get(
    "http://localhost:8000/api/v1/customers/",
    params={"workspace_id": "enterprise-workspace-1"},
    headers={"Authorization": "Bearer token123"}
)
print(f"Status: {response.status_code}")  # 200
print(f"Time: {response.elapsed.total_seconds():.2f}s")  # < 0.5s
print(response.json())
# {
#     "results": [
#         {
#             "id": "cust-001",
#             "name": "John Doe",
#             "workspace_id": "enterprise-workspace-1",
#             "latest_call": {
#                 "id": "call-999",
#                 "created_at": "2024-12-24T10:00:00Z",
#                 "duration_seconds": 180
#             },
#             "total_calls": 150,
#             "created_at": "2024-01-15T08:30:00Z"
#         },
#         ...
#     ],
#     "next": "http://localhost:8000/api/v1/customers/?cursor=eyJjcmVhdGVkX2F0Ijo...",
#     "previous": null
# }

# Profile endpoint
profile_response = client.get(
    "http://localhost:8000/api/v1/customers/",
    params={"workspace_id": "enterprise-workspace-1", "_profile": "true"}
)
print(profile_response.json()["_profiling"])
# {
#     "query_count": 5,
#     "total_query_time_ms": 45,
#     "serialization_time_ms": 120,
#     "total_time_ms": 185
# }
```

## Verification Commands

```bash
# Run performance tests
python manage.py test app.tests.test_performance -v 2

# Verify query count
python manage.py shell -c "
from app.profiling import profile_request
result = profile_request('/api/v1/customers/?workspace_id=large-ws')
assert result['query_count'] < 10, f'Too many queries: {result[\"query_count\"]}'
assert result['response_time_ms'] < 500, f'Too slow: {result[\"response_time_ms\"]}ms'
print('✅ Performance targets met!')
"

# Check indexes exist
python manage.py dbshell -c "
SELECT indexname FROM pg_indexes 
WHERE tablename IN ('customer', 'call') 
AND indexname LIKE 'idx_%';
"
```

## Deliverables

1. **Profiling Tools**: `/app/profiling/` with query analyzer, permission profiler, serializer profiler
2. **Migrations**: `/app/migrations/` with index creation migrations
3. **Optimized Views**: `/app/api/views.py` with N+1 fixes
4. **Cursor Pagination**: `/app/api/pagination.py` with CursorPagination implementation
5. **Permission Optimization**: `/app/api/permissions.py` with cached permission checks
6. **Caching Layer**: `/app/api/caching.py` with Redis integration
7. **Performance Tests**: `/app/tests/test_performance.py` validating all targets

## Notes

- Start with profiling to identify the actual bottlenecks before optimizing
- Use `django-debug-toolbar` or custom middleware for SQL query logging
- PostgreSQL `EXPLAIN ANALYZE` is your friend for query optimization
- Cursor pagination eliminates the O(n) offset problem
- `EXISTS` is always faster than `COUNT(*)` for boolean checks
- Consider partial indexes for hot data (e.g., recent 30 days)
- Test with realistic data volumes; small datasets hide performance issues
