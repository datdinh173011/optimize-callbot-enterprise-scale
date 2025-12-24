import os
import django
import uuid
from django.utils import timezone
from datetime import timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
django.setup()

from app.models import Customer, Call, Workspace

def seed():
    # Create test workspace
    workspace_id = uuid.UUID('11111111-1111-1111-1111-111111111111')
    workspace, created = Workspace.objects.get_or_create(
        id=workspace_id,
        defaults={'name': 'Enterprise Test Workspace'}
    )
    if created:
        print(f"Created workspace: {workspace.name}")
    else:
        print(f"Using existing workspace: {workspace.name}")

    # Create customers (10K for testing)
    print("Creating 10,000 test customers...")
    customers = []
    now = timezone.now()
    for i in range(10000):
        customers.append(Customer(
            workspace=workspace,
            name=f"Customer {i}",
            email=f"customer{i}@example.com",
            phone=f"+1555{i:07d}",
            status='active',
            tags=[],
            other_attributes={},
            is_deleted=False,
            created_at=now - timedelta(days=i % 365)
        ))

    Customer.objects.bulk_create(customers, ignore_conflicts=True)
    count = Customer.objects.filter(workspace=workspace).count()
    print(f"  Total customers in workspace: {count}")

    # Create calls (100K for testing)
    print("Creating 100,000 test calls...")
    customer_ids = list(Customer.objects.filter(workspace=workspace).values_list('id', flat=True)[:1000])
    if not customer_ids:
        print("Error: No customers found to link calls to.")
        return

    calls = []
    for i in range(100000):
        calls.append(Call(
            workspace=workspace,
            customer_id=customer_ids[i % len(customer_ids)],
            direction='inbound' if i % 2 == 0 else 'outbound',
            status='completed' if i % 3 != 0 else 'missed',
            duration=float(i % 300),
            call_type=['campaign', 'direct', 'callback'][i % 3],
            created_at=now - timedelta(hours=i)
        ))

    Call.objects.bulk_create(calls, ignore_conflicts=True)
    call_count = Call.objects.filter(workspace=workspace).count()
    print(f"  Total calls in workspace: {call_count}")

    print("\n✅ Test data seeded successfully")

if __name__ == "__main__":
    seed()
