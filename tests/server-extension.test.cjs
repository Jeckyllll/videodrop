const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const bridge=require('../extension-server/bridge.js');
test('remote message must come from the exact server and the embedded frame',()=>{
  const child={},frame={contentWindow:child};
  assert.equal(bridge.accepts({origin:bridge.base,source:child},frame),true);
  assert.equal(bridge.accepts({origin:bridge.base+'.evil.test',source:child},frame),false);
  assert.equal(bridge.accepts({origin:bridge.base,source:{}},frame),false);
});
test('remote UI cannot request arbitrary URLs or unrelated cookies',()=>{
  const sources=[{url:'https://edu.example/lesson'}];
  assert.equal(bridge.findSource(sources,'https://bank.example/'),undefined);
  assert.equal(bridge.findSource(sources,sources[0].url),sources[0]);
  const cookie=(domain,hostOnly=false)=>({domain,hostOnly,name:'session',path:'/',value:'private'});
  const values=bridge.scopedCookies([cookie('.example'),cookie('edu.example',true),cookie('bank.example',true),cookie('.example.evil.test'),cookie('edu.example',true)],['https://edu.example/lesson']);
  assert.deepEqual(values.map(c=>c.domain),['.example','edu.example']);
});
test('new package has no local host, debugger, Studio, shared secret or remote script execution',()=>{
  const root=path.join(__dirname,'../extension-server'),manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json')));
  assert.equal(manifest.permissions.includes('debugger'),false);
  assert.equal(manifest.permissions.includes('nativeMessaging'),false);
  for(const name of fs.readdirSync(root).filter(name=>/\.(js|json|html)$/.test(name))){
    const source=fs.readFileSync(path.join(root,name),'utf8');
    assert.doesNotMatch(source,/127\.0\.0\.1|localhost|studio-driver|VIDEODROP\.token|\beval\(|new Function\(/);
  }
  assert.match(manifest.content_security_policy.extension_pages,/script-src 'self'/);
});
