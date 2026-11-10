import tempfile
from pathlib import Path
from datapack.domain import ContentStore,manifest
def check(case):
    with tempfile.TemporaryDirectory() as d:
        store=ContentStore(d);sha=store.put({'kind':'data','value':'甲'})
        case.assertEqual(store.put({'value':'甲','kind':'data'}),sha)
        m=store.put(manifest([sha]));case.assertEqual(store.read(m)['refs'],[sha])
        store.path(sha).write_bytes(b'corrupt')
        with case.assertRaises(ValueError):store.read(sha)
        with case.assertRaises(ValueError):store.path('../bad')
