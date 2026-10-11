/**
 * 前端登录态行为测试（jsdom 真实执行页面脚本）
 *
 * 覆盖用户反馈的"总出现登录已失效"：
 *   1. 被踢下线后，登录表单必须原地重建（而不是留一个只读的用户条）
 *   2. 重建后表单要能重新提交（事件监听已重绑）
 *   3. 掉线原因要显示在表单上，不能只有一闪而过的 toast
 *
 * 注意：页面脚本里 `const state` / `function foo` 都在 <script> 的词法作用域里。
 * function 声明会挂到 window，但 const/let 不会 —— 所以对 state 的读写必须走 window.eval。
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

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:5000/',
  beforeParse(window) {
    // /api/me 一律当作"令牌已失效"，模拟掉线
    window.fetch = async (url) => {
      const u = String(url);
      if (u.includes('/api/me')) {
        return {
          status: 401, ok: false, headers: { get: () => null },
          clone: () => ({ json: async () => ({ success: false, error: '登录已失效，请重新登录', code: 'AUTH_REQUIRED' }) }),
          json: async () => ({ success: false, error: '登录已失效，请重新登录', code: 'AUTH_REQUIRED' }),
        };
      }
      const ok = { status: 200, ok: true, headers: { get: () => null },
                   json: async () => ({ success: true, items: [], total: 0, page: 1,
                                        categories: ['数码电子', '图书教材'], cats: {}, counts: {} }) };
      if (u.includes('/api/status')) return { ...ok, json: async () => ({ success: true, accounts_count: 10, block_number: 1 }) };
      return ok;
    };
    window.setInterval = () => 0;   // 别让心跳定时器挂住测试进程
    window.__errs = [];
    window.addEventListener('error', e => window.__errs.push(String(e.error || e.message)));
  },
});

const { window } = dom;
const doc = window.document;

setTimeout(() => {
  console.log('='.repeat(62));
  console.log('  前端登录态行为测试（jsdom 真实执行）');
  console.log('='.repeat(62));

  const card = doc.getElementById('authCard');
  check('页面存在 #authCard', !!card);

  // ---- 1. 关键函数都挂在 window 上 ----
  console.log('\n【1】页面脚本暴露的关键函数');
  ['renderAuthForm', 'bindAuthForm', 'clearAuth', 'setAuthHint', 'renderUserBar', 'doReset', 'submitAuth']
    .forEach(fn => check(`${fn}() 可调用`, typeof window[fn] === 'function'));

  // ---- 2. 登录 → 掉线 → 表单必须复原 ----
  console.log('\n【2】登录 → 掉线 → 表单必须复原');
  window.eval(`
    state.user = 'alice';
    state.info = { nickname: '爱丽丝', address: '0x1234', balance_eth: 99.9,
                   avg_rating: 4.5, sold_count: 1, bought_count: 2 };
    state.token = 'fake.token.value';
    renderUserBar();
  `);
  check('登录后卡片变成用户条（class=login-bar）', card.className.includes('login-bar'), card.className);
  check('用户条里能看到昵称', card.innerHTML.includes('爱丽丝'));
  check('用户条里没有用户名输入框', !doc.getElementById('loginName'));

  window.eval(`
    clearAuth();
    setAuthHint('登录已失效，请重新登录', 'warn');
    renderAuthForm();
  `);

  check('掉线后卡片变回 auth-card', card.className.includes('auth-card'), card.className);
  check('掉线后重新出现用户名输入框', !!doc.getElementById('loginName'));
  check('掉线后重新出现密码输入框', !!doc.getElementById('loginPassword'));
  check('掉线后重新出现提交按钮', !!doc.getElementById('authSubmit'));
  check('掉线后重新出现注册切换按钮', !!doc.getElementById('authSwitch'));

  // ---- 3. 掉线原因必须可见 ----
  console.log('\n【3】掉线原因必须可见');
  const msg = doc.getElementById('authMsg');
  check('#authMsg 存在', !!msg);
  check('提示文字包含"登录已失效"', msg && msg.textContent.includes('登录已失效'), msg && msg.textContent);
  check('提示条处于显示状态（class 含 show）', msg && msg.className.includes('show'), msg && msg.className);

  // ---- 4. 重建后的表单能再次提交 ----
  console.log('\n【4】重建后的表单可以再次提交');
  doc.getElementById('loginName').value = '';
  window.submitAuth();
  const m2 = doc.getElementById('authMsg');
  check('空用户名提交会给出前端校验提示（submitAuth 生效）',
        m2 && m2.textContent.includes('请输入用户名'), m2 && m2.textContent);

  // ---- 5. 登录/注册模式切换 ----
  console.log('\n【5】登录/注册模式切换');
  window.eval('state.authMode = "login"; switchAuthMode();');
  const nick = doc.getElementById('nicknameField');
  const strength = doc.getElementById('strengthBox');
  check('切到注册模式后昵称字段可见', nick && nick.style.display !== 'none', nick && nick.style.display);
  check('切到注册模式后强度条可见', strength && strength.style.display !== 'none', strength && strength.style.display);
  check('标题变为"注册新账号"', doc.getElementById('authTitle').textContent.includes('注册'));

  window.eval('switchAuthMode();');
  check('切回登录模式后昵称字段隐藏', nick.style.display === 'none', nick.style.display);

  // ---- 6. 掉线状态确实被清干净 ----
  console.log('\n【6】掉线后本地登录态已清空');
  const st = window.eval('({ token: state.token, user: state.user, ls: localStorage.getItem("ly_token") })');
  check('state.token 已清空', st.token === null, String(st.token));
  check('state.user 已清空', st.user === null, String(st.user));
  check('localStorage 里的令牌已清空', st.ls === null, String(st.ls));

  // ---- 7. doReset 不再整页刷新 ----
  console.log('\n【7】重置链不整页刷新');
  check('doReset() 不含 location.reload',
        !window.doReset.toString().includes('location.reload'));

  // ---- 8. 运行期没有未捕获异常 ----
  console.log('\n【8】页面脚本运行期无未捕获异常');
  const errs = window.__errs.filter(e => !/Not implemented|Could not parse CSS/.test(e));
  check('无未捕获 JS 异常', errs.length === 0, errs.join(' | '));

  console.log('\n' + '='.repeat(62));
  console.log(`  断言：${PASS} 通过 / ${FAIL} 失败  （共 ${PASS + FAIL} 条）`);
  if (failed.length) { console.log('  失败项：'); failed.forEach(f => console.log('    - ' + f)); }
  console.log('='.repeat(62));
  process.exit(FAIL ? 1 : 0);
}, 2500);
