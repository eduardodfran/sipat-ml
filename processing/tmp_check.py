import os
from supabase import create_client

url = os.environ['SUPABASE_URL']
key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
client = create_client(url, key)

# The supabase client has a postgrest client that can make raw HTTP requests
# Try using the internal _session (httpx client) to execute SQL via the Management API
# Since that fails, let's try using the rpc() method if a function exists

# Actually, let's just try to use the client.table('_sql') approach
# or check if the client has a .sql property

methods = [m for m in dir(client) if not m.startswith('_')]
print('Client methods:', methods)

# Check postgrest
pg = client.postgrest
pg_methods = [m for m in dir(pg) if not m.startswith('_')]
print('Postgrest methods:', pg_methods)

# Check if there's a rpc endpoint
try:
    # Try using the httpx session directly
    session = getattr(client, '_session', None)
    if session:
        print('Has _session')
    else:
        print('No _session')
except:
    pass
