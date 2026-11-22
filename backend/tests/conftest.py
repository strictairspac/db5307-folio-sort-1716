import os
import tempfile

_DB = tempfile.NamedTemporaryFile(prefix='folio-test-', suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DB_DRIVER', 'sqlite')
os.environ['DB_DRIVER'] = 'sqlite'
os.environ['DB_NAME'] = _DB.name
os.environ.setdefault('AUTH_KEY', 'test-auth-key')
os.environ['AUTH_KEY'] = 'test-auth-key'
os.environ.setdefault('SEED_DEMO', 'true')
