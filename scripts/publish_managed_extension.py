#!/usr/bin/env python3
"""Verify and atomically publish a staged managed Chrome release."""
import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature
from build_server_extension import FILES

NS = '{http://www.google.com/update2/response}'


def version_key(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){0,3}',value):
        raise ValueError('Invalid Chrome extension version.')
    parts=[int(x) for x in value.split('.')]
    if any(x>65535 for x in parts): raise ValueError('Invalid Chrome extension version.')
    return tuple(parts+[0]*(4-len(parts)))


def protobuf(data):
    def read_varint(offset):
        result=0
        for shift in range(0,70,7):
            if offset>=len(data): raise ValueError('Truncated CRX header.')
            byte=data[offset];offset+=1;result|=(byte & 127)<<shift
            if byte<128: return result,offset
        raise ValueError('Invalid CRX header.')
    fields={};offset=0
    while offset<len(data):
        tag,offset=read_varint(offset)
        if tag & 7 != 2: raise ValueError('Unsupported CRX header.')
        length,offset=read_varint(offset)
        if length>len(data)-offset: raise ValueError('Truncated CRX header.')
        fields.setdefault(tag>>3,[]).append(data[offset:offset+length]);offset+=length
    return fields


def parse_xml(data):
    if len(data)>32768 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Invalid update manifest.')
    try: root=ET.fromstring(data)
    except ET.ParseError as e: raise ValueError('Invalid update manifest.') from e
    apps=root.findall(NS+'app')
    if root.tag!=NS+'gupdate' or root.get('protocol')!='2.0' or len(apps)!=1 or len(root)!=1:
        raise ValueError('Invalid update manifest.')
    checks=apps[0].findall(NS+'updatecheck')
    if len(checks)!=1 or len(apps[0])!=1: raise ValueError('Invalid update manifest.')
    appid=apps[0].get('appid',''); version=checks[0].get('version',''); codebase=checks[0].get('codebase','')
    if not re.fullmatch(r'[a-p]{32}',appid): raise ValueError('Invalid extension identity.')
    version_key(version)
    return appid,version,codebase


def verify_release(folder,base,expected_id):
    folder=Path(folder);parts=urlsplit(base)
    if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or any(c in base for c in '<>"\' \r\n')): raise ValueError('Provide a clean HTTPS base URL.')
    base=base.rstrip('/')
    xml=(folder/'extension-update.xml').read_bytes()
    appid,version,codebase=parse_xml(xml)
    if appid!=expected_id: raise ValueError('Unexpected extension identity.')
    name=f'VideoDrop-{version}.crx'
    if codebase!=base+'/'+name: raise ValueError('Update manifest points to an unexpected URL.')
    path=folder/name
    if path.is_symlink() or not path.is_file() or path.stat().st_size>100*1024*1024:
        raise ValueError('Invalid signed package.')
    data=path.read_bytes()
    if len(data)<12 or data[:4]!=b'Cr24': raise ValueError('Invalid signed package.')
    crx_version,size=struct.unpack('<II',data[4:12])
    if crx_version!=3 or size>65536 or 12+size>=len(data): raise ValueError('Invalid signed package.')
    header=protobuf(data[12:12+size]); signed=header.get(10000,[]); proofs=header.get(2,[])
    if len(signed)!=1 or len(proofs)!=1: raise ValueError('Invalid CRX signature.')
    proof=protobuf(proofs[0]); public=proof.get(1,[]);signature=proof.get(2,[])
    if len(public)!=1 or len(signature)!=1: raise ValueError('Invalid CRX signature.')
    identity=hashlib.sha256(public[0]).digest()[:16]
    derived=''.join(chr(97+n) for b in identity for n in (b>>4,b&15))
    if derived!=appid or protobuf(signed[0]).get(1)!=[identity]: raise ValueError('CRX identity does not match.')
    try:
        key=serialization.load_der_public_key(public[0])
        if not isinstance(key,rsa.RSAPublicKey): raise ValueError('Unsupported CRX key.')
        key.verify(signature[0],b'CRX3 SignedData\0'+struct.pack('<I',len(signed[0]))+signed[0]+data[12+size:],padding.PKCS1v15(),hashes.SHA256())
    except (InvalidSignature,ValueError,TypeError) as e: raise ValueError('Invalid CRX signature.') from e
    try:
        with zipfile.ZipFile(io.BytesIO(data[12+size:])) as archive:
            entries=archive.infolist();names=[item.filename for item in entries]
            if len(names)!=len(FILES) or set(names)!=set(FILES) or sum(x.file_size for x in entries)>16*1024*1024:
                raise ValueError('Unexpected files in the extension package.')
            if archive.testzip() is not None: raise ValueError('Corrupt extension package.')
            manifest=json.loads(archive.read('manifest.json'))
    except (zipfile.BadZipFile,json.JSONDecodeError,UnicodeDecodeError) as e: raise ValueError('Invalid extension package.') from e
    if (not isinstance(manifest,dict) or not isinstance(manifest.get('permissions',[]),list)
            or manifest.get('version')!=version or manifest.get('update_url')!=base+'/extension-update.xml'
            or manifest.get('manifest_version')!=3
            or any(p in manifest.get('permissions',[]) for p in ('debugger','nativeMessaging'))):
        raise ValueError('Package and update manifest do not match.')
    return {'appid':appid,'version':version,'name':name,'crx':data,'xml':xml}


def publish(folder,destination,base,expected_id):
    if not re.fullmatch(r'[a-p]{32}',expected_id or ''): raise ValueError('Specify the reviewed extension identity.')
    release=verify_release(folder,base,expected_id)
    destination=Path(destination)
    if not destination.is_dir(): raise ValueError('Destination must be an existing directory.')
    update=destination/'extension-update.xml'; package=destination/release['name']
    # Serialize publishers and install the immutable package before switching the XML pointer.
    with (destination/'.videodrop-publish.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if update.exists():
            current_id,current_version,_=parse_xml(update.read_bytes())
            if current_id!=release['appid']: raise ValueError('Existing extension identity differs.')
            if version_key(release['version'])<version_key(current_version): raise ValueError('Refusing a version downgrade.')
            if version_key(release['version'])==version_key(current_version) and update.read_bytes()!=release['xml']:
                raise ValueError('Current version has a different update manifest.')
        if package.exists():
            if package.is_symlink() or package.read_bytes()!=release['crx']: raise ValueError('Versioned package already differs.')
        else:
            with tempfile.NamedTemporaryFile(dir=destination,prefix='.crx-',delete=False) as f:
                temporary=Path(f.name)
                try:
                    os.fchmod(f.fileno(),0o644);f.write(release['crx']);f.flush();os.fsync(f.fileno())
                    os.link(temporary,package)  # Atomic, cannot replace an existing version.
                finally: temporary.unlink(missing_ok=True)
        # Make the immutable package directory entry durable before switching the pointer.
        directory=os.open(destination,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        with tempfile.NamedTemporaryFile(dir=destination,prefix='.xml-',delete=False) as f:
            temporary=Path(f.name)
            try:
                os.fchmod(f.fileno(),0o644);f.write(release['xml']);f.flush();os.fsync(f.fileno())
                os.replace(temporary,update)
            finally: temporary.unlink(missing_ok=True)
        directory=os.open(destination,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    return package,update


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--staged',required=True);p.add_argument('--destination',required=True)
    p.add_argument('--base',required=True);p.add_argument('--expected-id',required=True)
    a=p.parse_args()
    for path in publish(a.staged,a.destination,a.base,a.expected_id): print(path)
