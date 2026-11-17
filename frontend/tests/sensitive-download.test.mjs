import test from 'node:test';
import assert from 'node:assert/strict';
import { downloadFilename, fetchSensitiveDownload } from '../src/lib/sensitiveDownload.js';

test('密码验证下载用请求头发送单次凭据和 CSRF，不进入地址', async () => {
  let request;
  const file=await fetchSensitiveDownload('/api/v1/backups/personal', 'once-only', 'csrf', 'backup.giftledger', async (url, options) => {
    request={url,options};return new Response(new Blob(['test']), {headers:{'Content-Disposition':"attachment; filename*=UTF-8''%E5%A4%87%E4%BB%BD.giftledger"}});
  });
  assert.equal(request.url, '/api/v1/backups/personal');
  assert.deepEqual(request.options.headers, {'X-Operation-Grant':'once-only','X-CSRF-Token':'csrf'});
  assert.equal(request.options.credentials, 'include');assert.equal(file.filename,'备份.giftledger');
  assert.equal(await file.blob.text(),'test');
});
test('无凭据或 CSRF 不发起下载，拒绝或网络失败不生成可保存文件', async () => {
  let calls=0;
  await assert.rejects(fetchSensitiveDownload('/download','','csrf','backup',async()=>{calls++;}),/重新验证/);
  await assert.rejects(fetchSensitiveDownload('/download','grant','','backup',async()=>{calls++;}),/重新验证/);
  assert.equal(calls,0);
  await assert.rejects(fetchSensitiveDownload('/download','grant','csrf','backup',async()=>new Response(JSON.stringify({error:{message:'凭据已使用',code:'OPERATION_GRANT_INVALID'}}),{status:403})),{message:'凭据已使用',status:403,code:'OPERATION_GRANT_INVALID'});
  await assert.rejects(fetchSensitiveDownload('/download','grant','csrf','backup',async()=>{throw new Error('offline');}),/重新验证后重试/);
});
test('下载文件名清除目录与控制字符，非法编码安全回退',()=>{
  assert.equal(downloadFilename('attachment; filename="../backup.giftledger"','safe'),'.._backup.giftledger');
  assert.equal(downloadFilename("attachment; filename*=UTF-8''%ZZ",'safe'),'safe');
});
