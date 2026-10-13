import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { test } from 'node:test';
import { parse } from 'node:url';
import { proxyRequest } from 'next/dist/server/lib/router-utils/proxy-request';

async function listen(server:Server):Promise<string>{
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

test('development proxy carries a checked-file response beyond Next default 30 seconds', {timeout:45_000}, async()=>{
  let timer:ReturnType<typeof setTimeout>|undefined;
  const backend=createServer((_request,response)=>{
    timer=setTimeout(()=>{response.writeHead(200,{'Content-Type':'application/json'});response.end('{"state":"READY"}');},31_000);
  });
  const originalTarget=process.env.CQ_DEV_API_PROXY;
  let proxy:Server|undefined;
  try{
    const target=await listen(backend);process.env.CQ_DEV_API_PROXY=target;
    const config=(await import('../../next.config')).default;
    const rules=await config.rewrites!();
    assert.ok(Array.isArray(rules));
    const destination=rules.find(rule=>rule.source==='/api/v1/:path*')!.destination;
    const upstream=destination.replace('/api/v1/:path*','');
    proxy=createServer((request,response)=>{
      void proxyRequest(request,response,parse(`${upstream}${request.url}`,true),undefined,undefined,config.experimental?.proxyTimeout).catch(()=>{
        if(!response.writableEnded){response.writeHead(500);response.end();}
      });
    });
    const origin=await listen(proxy);
    // Real installed Next transport, not a browser interception/delayed mock.
    const response=await fetch(`${origin}/api/v1/ie/me/talent-profile/files/mock/complete`,{method:'POST',signal:AbortSignal.timeout(40_000)});
    assert.equal(response.status,200,'bounded file checks may legitimately exceed the dev proxy default');
    assert.deepEqual(await response.json(),{state:'READY'});
  }finally{
    if(timer)clearTimeout(timer);
    if(proxy){proxy.closeAllConnections();await new Promise<void>(resolve=>proxy!.close(()=>resolve()));}
    backend.closeAllConnections();await new Promise<void>(resolve=>backend.close(()=>resolve()));
    if(originalTarget===undefined)delete process.env.CQ_DEV_API_PROXY;else process.env.CQ_DEV_API_PROXY=originalTarget;
  }
});
