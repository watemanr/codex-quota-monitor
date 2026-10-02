'use strict';
const $ = selector => document.querySelector(selector);
const state = {csrf:'', accounts:[], settings:{}, recent:{}, defaults:{}};
const apiURL = path => new URL('../' + path, location.href).href;
const timeText = seconds => seconds == null ? '—' : new Intl.DateTimeFormat('zh-CN', {
  timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
  hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false
}).format(new Date(seconds * 1000));
function status(message, failed=false) {
  const node = $('#bark-status');
  node.textContent = message;
  node.classList.toggle('error', failed);
}
async function api(path, options={}) {
  const headers = {Accept:'application/json', ...options.headers};
  if (options.method === 'POST') headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(apiURL(path), {...options, headers, credentials:'same-origin'});
  let result;
  try { result = await response.json(); }
  catch { throw new Error('服务器响应异常（' + response.status + '）'); }
  if (!response.ok) {
    const error = new Error(result.error || '请求失败（' + response.status + '）');
    error.status = response.status;
    throw error;
  }
  return result;
}
function selected() { return $('#bark-account').value; }
function setting() { return state.settings[selected()] || state.defaults; }
function renderRecent() {
  const root = $('#bark-recent');
  root.replaceChildren();
  const rows = state.recent[selected()] || [];
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '尚无发送记录。';
    root.append(empty);
    return;
  }
  const kinds = {reset:'重置提醒',low:'低额度警告',report:'定时报告',tibo:'Tibo 动态',test:'测试'};
  const states = {pending:'待发送',sending:'发送中',sent:'已发送',failed:'失败',canceled:'已取消'};
  for (const row of rows) {
    const line = document.createElement('div');
    line.className = 'bark-log';
    const main = document.createElement('span');
    main.textContent = (kinds[row.kind] || row.kind) + ' · ' + (states[row.status] || row.status);
    const detail = document.createElement('small');
    detail.textContent = timeText(row.sent_at || row.created_at) +
      (row.last_error ? ' · ' + row.last_error : '') +
      (row.status === 'pending' && row.attempts ? ' · 等待重试' : '');
    line.append(main, detail);
    root.append(line);
  }
}
function render() {
  const current = setting();
  $('#bark-url').value = current.server_url || '';
  $('#bark-key').value = '';
  $('#bark-key-hint').textContent = current.key_configured ?
    '已保存 key ' + (current.key_hint || '') + '；留空保持不变。' : '首次保存时必须填写 key。';
  $('#bark-group').value = current.group_name || 'Codex额度';
  $('#bark-enabled').checked = !!current.enabled;
  $('#bark-5h-reset').checked = !!current.notify_5h_reset;
  $('#bark-weekly-reset').checked = !!current.notify_weekly_reset;
  $('#bark-5h-low').checked = !!current.notify_5h_low;
  $('#bark-weekly-low').checked = !!current.notify_weekly_low;
  $('#bark-tibo-posts').checked = !!current.notify_tibo_posts;
  $('#bark-threshold').value = current.low_threshold ?? 5;
  $('#bark-interval').value = current.report_interval_minutes ?? 0;
  $('#bark-test').disabled = !current.key_configured;
  $('#bark-remove').disabled = !current.key_configured;
  renderRecent();
}
async function load(preferred=selected()) {
  const result = await api('api/bark/settings');
  state.accounts = result.accounts;
  state.settings = result.settings;
  state.recent = result.recent;
  state.defaults = result.defaults;
  const selector = $('#bark-account');
  selector.replaceChildren();
  for (const account of result.accounts) selector.append(new Option(account, account));
  if (result.accounts.includes(preferred)) selector.value = preferred;
  $('#bark-form').hidden = result.accounts.length === 0;
  if (!result.accounts.length) status('还没有 Codex 账号；先上传一条额度数据。');
  else status('');
  render();
}
async function post(path, payload, success) {
  const buttons = [...document.querySelectorAll('.bark-actions button')];
  buttons.forEach(button => button.disabled = true);
  status('正在处理…');
  try {
    await api(path, {method:'POST', headers:{'Content-Type':'application/json'},
                     body:JSON.stringify(payload)});
    await load(payload.account);
    status(success);
  } catch (error) {
    status(error.message, true);
    if (error.status === 401) $('#bark-login').hidden = false;
  } finally {
    buttons.forEach(button => button.disabled = false);
    $('#bark-test').disabled = !setting().key_configured;
    $('#bark-remove').disabled = !setting().key_configured;
  }
}
$('#bark-account').addEventListener('change', render);
$('#bark-form').addEventListener('submit', async event => {
  event.preventDefault();
  const account = selected();
  const payload = {
    account,
    server_url:$('#bark-url').value.trim(),
    device_key:$('#bark-key').value.trim(),
    group_name:$('#bark-group').value.trim(),
    enabled:$('#bark-enabled').checked,
    notify_5h_reset:$('#bark-5h-reset').checked,
    notify_weekly_reset:$('#bark-weekly-reset').checked,
    notify_5h_low:$('#bark-5h-low').checked,
    notify_weekly_low:$('#bark-weekly-low').checked,
    notify_tibo_posts:$('#bark-tibo-posts').checked,
    low_threshold:Number($('#bark-threshold').value),
    report_interval_minutes:Number($('#bark-interval').value),
  };
  await post('api/bark/settings', payload, '设置已保存。');
});
$('#bark-test').addEventListener('click', () =>
  post('api/bark/test', {account:selected()}, '测试通知已加入发送队列；稍后可刷新发送状态。'));
$('#bark-refresh').addEventListener('click', async () => {
  try { await load(); status('发送状态已刷新。'); }
  catch (error) { status(error.message, true); }
});
$('#bark-remove').addEventListener('click', async () => {
  const account = selected();
  if (!confirm('删除 ' + account + ' 的 Bark 地址和 key？未发送通知也会取消。')) return;
  await post('api/bark/remove', {account}, 'Bark 配置已删除。');
});
(async () => {
  try {
    const session = await api('api/session');
    if (!session.authenticated) {
      $('#bark-login').hidden = false;
      status('请先返回看板登录。', true);
      return;
    }
    state.csrf = session.csrf_token;
    await load();
    $('#bark-content').hidden = false;
  } catch (error) {
    if (error.status === 401) $('#bark-login').hidden = false;
    status('读取推送设置失败：' + error.message, true);
  }
})();
