import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build_managed_extension import stage


def protobuf(data):
    def varint(offset):
        result = shift = 0
        while True:
            byte = data[offset]; offset += 1
            result |= (byte & 127) << shift
            if byte < 128: return result, offset
            shift += 7
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = varint(pos)
        if tag & 7 != 2: raise ValueError('unexpected protobuf wire type')
        size, pos = varint(pos)
        result.setdefault(tag >> 3, []).append(data[pos:pos+size])
        pos += size
    return result


@unittest.skipUnless((ROOT / 'scripts/node_modules/crx3/bin/crx3.js').is_file(), 'Install release dependencies with npm ci --prefix scripts')
class ManagedReleaseTests(unittest.TestCase):
    def test_signed_package_identity_manifest_and_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); key = root / 'private.pem'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                            '-out', str(key)], check=True, capture_output=True)
            key.chmod(0o600)
            expected = None
            for index in range(2):
                crx, xml = stage(key, root / f'release-{index}', 'https://video.example/app')
                data = crx.read_bytes()
                self.assertEqual(data[:4], b'Cr24')
                version, size = struct.unpack('<II', data[4:12]); self.assertEqual(version, 3)
                header = protobuf(data[12:12+size]); signed = header[10000][0]
                proof = protobuf(header[2][0]); public, signature = proof[1][0], proof[2][0]
                identity = hashlib.sha256(public).digest()[:16]
                self.assertEqual(protobuf(signed)[1][0], identity)
                appid = ''.join(chr(97 + nibble) for byte in identity for nibble in (byte >> 4, byte & 15))
                if expected: self.assertEqual(expected, appid)
                expected = appid
                pub = root / 'public.der'; sig = root / 'signature'; payload = root / 'signed'
                pub.write_bytes(public); sig.write_bytes(signature)
                payload.write_bytes(b'CRX3 SignedData\0' + struct.pack('<I', len(signed)) + signed + data[12+size:])
                verify = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(pub), '-keyform', 'DER',
                                         '-signature', str(sig), str(payload)], capture_output=True)
                self.assertEqual(verify.returncode, 0, verify.stderr.decode())
                with zipfile.ZipFile(io.BytesIO(data[12+size:])) as archive:
                    manifest = json.loads(archive.read('manifest.json'))
                    self.assertEqual(manifest['update_url'], 'https://video.example/app/extension-update.xml')
                    self.assertNotIn('private.pem', archive.namelist())
                    self.assertIn('stream-core.mjs', archive.namelist())
                ns = {'u': 'http://www.google.com/update2/response'}
                app = ET.parse(xml).find('.//u:app', ns)
                self.assertEqual(app.get('appid'), appid)
                update = app.find('u:updatecheck', ns)
                self.assertEqual(update.get('version'), manifest['version'])
                self.assertEqual(crx.name,'VideoDrop-'+manifest['version']+'.crx')
                self.assertEqual(update.get('codebase'), 'https://video.example/app/'+crx.name)

    def test_rejects_missing_or_exposed_key_and_non_https(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); key = root / 'private.pem'; key.write_text('not a real key')
            key.chmod(0o644)
            with self.assertRaises(ValueError): stage(key, root / 'out', 'https://video.example/app')
            key.chmod(0o600)
            with self.assertRaises(ValueError): stage(key, root / 'out', 'http://video.example/app')
            with self.assertRaises(ValueError): stage(root / 'missing', root / 'out', 'https://video.example/app')
            with self.assertRaises(ValueError): stage(key, root, 'https://video.example/app')

    def test_two_versions_keep_one_identity_and_distinct_package_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);key=root/'private.pem'
            subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048',
                            '-out',str(key)],check=True,capture_output=True)
            key.chmod(0o600)
            project=root/'project';shutil.copytree(ROOT/'extension-server',project/'extension-server')
            manifest_file=project/'extension-server/manifest.json'
            manifest=json.loads(manifest_file.read_text()); results=[]
            tool=ROOT/'scripts/node_modules/crx3/bin/crx3.js'
            for version in ('2.4.0','2.4.1'):
                manifest['version']=version;manifest_file.write_text(json.dumps(manifest))
                with patch('build_managed_extension.ROOT',project):
                    crx,xml=stage(key,root/('release-'+version),'https://video.example/app',crx_tool=tool)
                ns={'u':'http://www.google.com/update2/response'}
                app=ET.parse(xml).find('.//u:app',ns); update=app.find('u:updatecheck',ns)
                self.assertEqual(crx.name,'VideoDrop-'+version+'.crx')
                self.assertEqual(update.get('codebase'),'https://video.example/app/'+crx.name)
                results.append(app.get('appid'))
            self.assertEqual(results[0],results[1])


if __name__ == '__main__': unittest.main()
