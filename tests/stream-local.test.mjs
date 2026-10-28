import test from 'node:test';
import assert from 'node:assert/strict';
import {checkedFetch} from '../extension-server/stream-core.mjs';

test('HLS requests stay on the selected origin and cannot follow redirects',async()=>{
  const calls=[];
  const fetch=checkedFetch('https://media.example/course/master.m3u8',async (...args)=>{calls.push(args);return 'ok';});
  assert.equal(await fetch('https://media.example/course/1.ts',{redirect:'follow'}),'ok');
  assert.equal(calls[0][1].redirect,'error');
  assert.ok(calls[0][1].signal instanceof AbortSignal);
  const cancelled=new AbortController();cancelled.abort();
  await fetch('https://media.example/course/2.ts',{signal:cancelled.signal});
  assert.equal(calls[1][1].signal.aborted,true);
  assert.throws(()=>fetch('https://other.example/secret'),/другому сайту/);
  assert.throws(()=>fetch('http://media.example/course/1.ts'),/другому сайту/);
  assert.throws(()=>fetch('file:///etc/passwd'),/другому сайту/);
});
