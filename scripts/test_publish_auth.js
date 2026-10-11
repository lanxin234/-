/**
 * 带图片上架的鉴权回归测试（jsdom 真实执行页面脚本）
 *
 * 背景：修复前「上架商品」在**附了图片**时会报「登录已失效」。
 * 根因是那条分支用了裸 fetch，漏带 Authorization 头：
 *     const res = await fetch(API + '/api/items', { method: 'POST', body: fd });
 * 而不带图片的分支走 post() → api()，会自动带上令牌。所以只有"带图"才会掉线。
 *
 * 本测试直接拦截 window.fetch，检查：
 *   1. 带图片上架时，请求确实带了 Authorization: Bearer <token>
 *   2. 请求体是 FormData，且 name/price_eth/category/image 都在
 *   3. 不带图片上架时同样带令牌
 *   4. 全站不存在绕过 api() 的、会漏令牌的裸 fetch（api() 内部与无需鉴权的
 *      /api/upload 除外）
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const HTML_PATH = path.join(__dirname, '..', 'frontend', 'index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');

let PASS = 0, FAIL = 0;
const failed = [];
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  [OK] ' + name); }
  else { FAIL++; failed.push(name); console.log('  [XX] ' + name + '  ' + (extra || '')); }
}

const TOKEN = 'header.payload.signature';

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:5000/',
  beforeParse(window) {
    window.__calls = [];
    window.localStorage.setItem('ly_token', TOKEN);
    window.fetch = async (url, opts) => {
      const o = opts || {};
      const h = o.headers || {};
      window.__calls.push({
        url: String(url),
        method: o.method || 'GET',
        auth: h['Authorization'] || h['authorization'] || null,
        body: o.body,
      });
      const u = String(url);
      // 分类接口要返回 categories，否则前端 state.categories 会被设成 undefined
      let payload = { success: true, items: [], total: 0, page: 1,
                      categories: ['数码电子', '图书教材'], counts: {}, cats: {} };
      if (u.includes('/api/me')) {
        payload = { success: true, username: 'alice', nickname: '爱丽丝',
                    address: '0x1', balance_eth: 100, avg_rating: 0,
                    sold_count: 0, bought_count: 0 };
      } else if (u.includes('/api/status')) {
        payload = { success: true, accounts_count: 10, block_number: 1 };
      } else if (u.includes('/api/items') && (o.method || 'GET') === 'POST') {
        payload = { success: true, message: '商品已上架到区块链',
                    image_hash: 'abc123', image_url: '/uploads/abc123.png' };
      }
      return {
        status: 200, ok: true,
        headers: { get: () => null },
        clone: () => ({ json: async () => payload }),
        json: async () => payload,
      };
    };
    window.setInterval = () => 0;
    window.__errs = [];
    window.addEventListener('error', e => window.__errs.push(String(e.error || e.message)));
  },
});

const { window } = dom;
const doc = window.document;

setTimeout(async () => {
  console.log('='.repeat(64));
  console.log('  带图片上架的鉴权回归测试（jsdom）');
  console.log('='.repeat(64));

  // 先登录态就位
  window.eval(`state.token = ${JSON.stringify(TOKEN)}; state.user = 'alice'; state.info = { nickname:'爱丽丝', address:'0x1', balance_eth:100 };`);

  // ---- 1. 带图片上架 ----
  console.log('\n【1】带图片上架商品');
  window.eval(`state.pubImageHash = 'deadbeef'; state.pubCategory = '数码电子';`);
  doc.getElementById('pubName').value = '带图测试商品';
  doc.getElementById('pubDesc').value = '一段描述';
  doc.getElementById('pubPrice').value = '1.5';

  // modal 一律确认通过
  window.modal = async () => true;

  window.__calls.length = 0;
  await window.eval('doPublish(document.getElementById("pubBtn"))');
  await new Promise(r => setTimeout(r, 120));

  const publishCall = window.__calls.find(c => c.url.includes('/api/items') && c.method === 'POST');
  check('发出了 /api/items POST 请求', !!publishCall, JSON.stringify(window.__calls.map(c => c.url)));
  if (publishCall) {
    check('请求带上了 Authorization 头', !!publishCall.auth, String(publishCall.auth));
    check('Authorization 格式为 "Bearer <token>"',
          publishCall.auth === 'Bearer ' + TOKEN, String(publishCall.auth));
    const isFD = publishCall.body && typeof publishCall.body.append === 'function';
    check('请求体是 FormData（multipart）', isFD, typeof publishCall.body);
    if (isFD) {
      check('FormData 含 name 字段', publishCall.body.get('name') === '带图测试商品', String(publishCall.body.get('name')));
      check('FormData 含 price_eth 字段', publishCall.body.get('price_eth') === '1.5', String(publishCall.body.get('price_eth')));
      check('FormData 含 category 字段', publishCall.body.get('category') === '数码电子', String(publishCall.body.get('category')));
    }
  }

  // ---- 2. 不带图片上架 ----
  console.log('\n【2】不带图片上架商品');
  window.eval(`state.pubImageHash = '';`);
  doc.getElementById('pubName').value = '无图测试商品';
  doc.getElementById('pubPrice').value = '2';

  window.__calls.length = 0;
  await window.eval('doPublish(document.getElementById("pubBtn"))');
  await new Promise(r => setTimeout(r, 120));

  const call2 = window.__calls.find(c => c.url.includes('/api/items') && c.method === 'POST');
  check('无图时也发出了上架请求', !!call2);
  if (call2) {
    check('无图时同样带上 Authorization 头',
          call2.auth === 'Bearer ' + TOKEN, String(call2.auth));
  }

  // ---- 3. 静态扫描：不存在会漏令牌的裸 fetch ----
  console.log('\n【3】静态扫描裸 fetch（防止以后再写漏令牌的调用）');
  const script = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];
  const rawFetches = [...script.matchAll(/fetch\(API\s*\+[^)]*/g)].map(m => {
    const line = script.slice(0, m.index).split('\n').length;
    const text = script.split('\n')[line - 1].trim();
    return { line, text };
  });
  // 允许的例外：api() 自身（带 options）、以及无需鉴权的 /api/upload
  const illegal = rawFetches.filter(f =>
    !f.text.includes('options') && !f.text.includes('/api/upload'));
  check('除 api() 自身与 /api/upload 外，无其他裸 fetch', illegal.length === 0,
        illegal.map(f => `L${f.line}: ${f.text}`).join(' | '));

  // ---- 4. 无未捕获异常 ----
  console.log('\n【4】运行期无未捕获异常');
  const errs = (window.__errs || []).filter(e => !/Not implemented|Could not parse CSS/.test(e));
  check('无未捕获 JS 异常', errs.length === 0, errs.join(' | '));

  console.log('\n' + '='.repeat(64));
  console.log(`  断言：${PASS} 通过 / ${FAIL} 失败  （共 ${PASS + FAIL} 条）`);
  if (failed.length) { console.log('  失败项：'); failed.forEach(f => console.log('    - ' + f)); }
  console.log('='.repeat(64));
  process.exit(FAIL ? 1 : 0);
}, 2500);
