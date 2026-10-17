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

setTimeout(async () => {
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

  // ---- 8. 「记住我」 ----
  console.log('\n【8】记住我：勾选框 + 用户名记忆（密码绝不落本地）');
  const rm = doc.getElementById('rememberMe');
  check('登录表单里有「记住我」勾选框', !!rm);

  if (rm) {
    rm.checked = true;
    check('isRememberChecked() 能读到勾选状态', window.isRememberChecked() === true);
    rm.checked = false;
    check('取消勾选后 isRememberChecked() 为 false', window.isRememberChecked() === false);
  }

  // 记住用户名的读写
  window.eval('setRememberedUser("小明")');
  check('setRememberedUser 后能读回用户名', window.getRememberedUser() === '小明',
        window.getRememberedUser());
  check('记住的用户名存在 localStorage.ly_user',
        window.localStorage.getItem('ly_user') === '小明');
  check('密码没有被写进 localStorage',
        !JSON.stringify(window.localStorage).includes('password'),
        '发现 password 字段');

  window.eval('setRememberedUser("")');
  check('传空串能清掉记住的用户名', window.getRememberedUser() === '');

  // 主动登出应该连用户名一起忘掉
  window.eval('localStorage.setItem("ly_token", "fake"); setRememberedUser("小明"); clearAuth(true);');
  check('clearAuth(true) 会清掉记住的用户名',
        window.localStorage.getItem('ly_user') === null,
        String(window.localStorage.getItem('ly_user')));
  check('clearAuth(true) 也清掉令牌',
        window.localStorage.getItem('ly_token') === null);

  // 令牌过期导致的被动清理，不该顺手清掉用户名
  window.eval('localStorage.setItem("ly_token", "fake"); setRememberedUser("小明"); clearAuth();');
  check('clearAuth() 不带参数时保留用户名（免得重敲）',
        window.localStorage.getItem('ly_user') === '小明',
        String(window.localStorage.getItem('ly_user')));
  window.eval('localStorage.removeItem("ly_token"); setRememberedUser("")');

  // ---- 10. 手机端底部导航 ----
  // 用户反馈"手机端找不到上架商品的入口"。根因是 ≤620px 时写了
  // `nav { display: none }`，把顶部导航整个隐藏了，又没有替代入口。
  // 这里同时校验 DOM 和 CSS，防止再写回那种写法。
  console.log('\n【10】手机端底部导航（上架入口可达性）');
  const mobileNav = doc.getElementById('mobileNav');
  check('存在手机端导航 #mobileNav', !!mobileNav);

  const mBtns = mobileNav ? [...mobileNav.querySelectorAll('button')] : [];
  check('底部导航有 6 个入口（含默认隐藏的管理）', mBtns.length === 6, String(mBtns.length));

  const mViews = mBtns.map(b => b.dataset.view);
  check('包含 publish（上架）入口', mViews.includes('publish'), mViews.join(','));
  check('market/orders/board/chain 都在',
        ['market', 'orders', 'board', 'chain'].every(v => mViews.includes(v)),
        mViews.join(','));

  // 管理入口默认不可见：非管理员不该看到它
  const mnAdmin = mBtns.find(b => b.dataset.view === 'admin');
  check('管理入口默认隐藏', !!mnAdmin && mnAdmin.style.display === 'none',
        mnAdmin ? mnAdmin.style.display : 'missing');
  check('桌面端管理入口也默认隐藏',
        doc.getElementById('navAdmin').style.display === 'none');

  check('没有把整个 nav 隐藏（曾经的 bug）',
        !/\bnav\s*\{\s*display:\s*none/.test(html));
  check('620px 断点隐藏的是 #mainNav',
        html.includes('#mainNav { display: none; }'));

  const mnBlocks = [...html.matchAll(/#mobileNav\s*\{([^}]*)\}/g)].map(m => m[1]);
  check('底部导航使用 fixed 定位', mnBlocks.some(b => /position:\s*fixed/.test(b)),
        mnBlocks.join(' | ').slice(0, 120));

  // 切视图时两套导航要同步高亮
  window.eval("switchView('publish')");
  check('切换后底部导航「上架」高亮',
        doc.querySelector('#mobileNav button[data-view="publish"]').classList.contains('active'));
  check('顶部导航「发布商品」同步高亮',
        doc.querySelector('#mainNav button[data-view="publish"]').classList.contains('active'));
  check('发布视图已激活',
        doc.getElementById('view-publish').classList.contains('active'));

  // ---- 11. 管理页渲染与操作接线 ----
  console.log('\n【11】管理页渲染与操作接线');
  const savedFetch = window.fetch;
  window.__adminCalls = [];
  window.fetch = async (url, opts) => {
    const u = String(url);
    const o = opts || {};
    window.__adminCalls.push({ url: u, method: o.method || 'GET', body: o.body, headers: o.headers || {} });
    let payload = { success: true };
    if (u.includes('/api/admin/session')) {
      payload = { success: true, admin: true, mode: 'local' };
    } else if (u.includes('/api/admin/users')) {
      payload = {
        success: true, count: 1,
        platform: { address: '0x0', balance_eth: 100 },
        users: [{ username: '甲', nickname: '甲', address: '0xabc', balance_eth: 10,
                  sold_count: 1, bought_count: 0, avg_rating: 5,
                  listed_count: 2, on_sale_count: 1 }],
      };
    } else if (u.includes('/api/admin/items')) {
      payload = { success: true, count: 1, items: [{ item_id: 1, name: '违规商品',
                  price_eth: 1, seller_name: '甲', seller_username: '甲', status: 0 }] };
    } else if (u.includes('/api/admin/logs')) {
      payload = { success: true, logs: [{ action: 'recharge', target: '甲', amount: '5',
                  balance_after: 15, by: 'local', time: '2026-10-17 10:00:00' }] };
    } else if (u.includes('/api/admin/recharge')) {
      payload = { success: true, message: '已充值' };
    } else {
      payload = { success: true, items: [], total: 0, page: 1,
                  categories: [], counts: {}, cats: {} };
    }
    return { status: 200, ok: true, headers: { get: () => null },
             clone: () => ({ json: async () => payload }), json: async () => payload };
  };

  window.eval('state.isAdmin = true; loadAdmin();');
  await new Promise(r => setTimeout(r, 80));
  check('用户表渲染出用户名',
        doc.getElementById('adminUserTable').textContent.includes('甲'),
        doc.getElementById('adminUserTable').textContent.slice(0, 80));
  check('商品表渲染出商品名',
        doc.getElementById('adminItemTable').textContent.includes('违规商品'));
  check('审计日志渲染出操作',
        doc.getElementById('adminLogList').textContent.includes('充值'));
  const rmBtn = doc.querySelector('#adminItemTable .btn-mini.danger');
  check('在售商品的下架按钮可用', !!rmBtn && !rmBtn.disabled);

  // 充值：确认请求体带对了 username / amount
  window.prompt = () => '5';
  window.eval('doRecharge("甲")');
  await new Promise(r => setTimeout(r, 80));
  const rechargeCall = window.__adminCalls.find(c => c.url.includes('/api/admin/recharge'));
  let body = {};
  try { body = JSON.parse(rechargeCall.body); } catch (e) {}
  check('充值请求带 username', body.username === '甲', String(rechargeCall && rechargeCall.body));
  check('充值请求带金额 5', body.amount_eth === 5, String(body && body.amount_eth));
  check('充值后重新拉取用户列表',
        window.__adminCalls.filter(c => c.url.includes('/api/admin/users')).length >= 2);

  window.fetch = savedFetch;
  window.eval("switchView('market')");

  // ---- 12. 运行期没有未捕获异常 ----
  console.log('\n【12】页面脚本运行期无未捕获异常');
  const errs = window.__errs.filter(e => !/Not implemented|Could not parse CSS/.test(e));
  check('无未捕获 JS 异常', errs.length === 0, errs.join(' | '));

  console.log('\n' + '='.repeat(62));
  console.log(`  断言：${PASS} 通过 / ${FAIL} 失败  （共 ${PASS + FAIL} 条）`);
  if (failed.length) { console.log('  失败项：'); failed.forEach(f => console.log('    - ' + f)); }
  console.log('='.repeat(62));
  process.exit(FAIL ? 1 : 0);
}, 2500);
