'use strict';
const $ = selector => document.querySelector(selector);
const state = {csrf:''};
const apiURL = path => new URL('../' + path, location.href).href;
function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = content;
  return node;
}
function status(message, failed=false) {
  const node = $('#account-management-status');
  node.textContent = message;
  node.classList.toggle('error', failed);
}
async function api(path, options={}) {
  const headers = {Accept:'application/json', ...options.headers};
  if (options.method === 'POST' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(apiURL(path), {...options, credentials:'same-origin', headers});
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
function fillSelect(id, accounts, preferred) {
  const select = $(id), previous = select.value;
  select.replaceChildren();
  for (const account of accounts) select.append(new Option(account.account, account.account));
  const names = accounts.map(account => account.account);
  select.value = names.includes(preferred) ? preferred : names.includes(previous) ? previous : names[0] || '';
}
async function loadAccounts(preferred='') {
  const result = await api('api/accounts/manage');
  const accounts = result.accounts;
  const root = $('#managed-accounts');
  root.replaceChildren();
  if (!accounts.length) root.append(element('p', 'muted', '当前没有账号。'));
  for (const account of accounts) {
    const row = element('div', 'managed-row');
    row.append(element('strong', '', account.account),
               element('span', '', account.samples + ' 条额度采样 · ' + account.events + ' 条变化 · ' + account.analytics_reports + ' 份分析上报'));
    if (account.aliases.length) {
      const aliases = element('div', 'managed-alias-list');
      for (const alias of account.aliases) {
        const line = element('div', 'managed-alias');
        const button = element('button', 'secondary', '解除占用');
        button.type = 'button';
        button.setAttribute('aria-label', '解除 ' + alias + ' 的占用');
        button.addEventListener('click', () =>
          postAction('release-alias', {account:account.account, alias}, account.account,
            alias + ' 已解除占用；以后用此名称上传会生成独立账号。', button));
        line.append(element('span', '', '旧名称：' + alias), button);
        aliases.append(line);
      }
      row.append(aliases);
    }
    root.append(row);
  }
  for (const id of ['#rename-account','#merge-source','#merge-target','#delete-account'])
    fillSelect(id, accounts, preferred);
  if ($('#merge-source').value === $('#merge-target').value && accounts.length > 1)
    $('#merge-target').value = accounts.find(account => account.account !== $('#merge-source').value).account;
  $('#merge-submit').disabled = accounts.length < 2;
  const blocked = $('#blocked-accounts');
  blocked.replaceChildren();
  if (!result.deleted_names.length) blocked.append(element('p', 'muted', '没有已删除的名称。'));
  for (const name of result.deleted_names) {
    const row = element('div', 'blocked-row');
    const button = element('button', 'secondary', '允许重新使用');
    button.type = 'button';
    button.addEventListener('click', () =>
      postAction('unblock', {account:name}, '', name + ' 已允许重新使用；新数据上报后才会重新出现。', button));
    row.append(element('span', '', name), button);
    blocked.append(row);
  }
}
async function postAction(action, payload, preferred, message, button) {
  button.disabled = true;
  status('正在保存…');
  let completed = false;
  try {
    await api('api/accounts/' + action, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });
    completed = true;
    await loadAccounts(preferred);
    status(message);
    return true;
  } catch (error) {
    status((completed ? '操作已完成，但列表刷新失败：' : '') + error.message, true);
    if (error.status === 401) $('#management-login').hidden = false;
    return false;
  } finally {
    button.disabled = false;
  }
}
$('#rename-form').addEventListener('submit', async event => {
  event.preventDefault();
  const account = $('#rename-account').value, newName = $('#rename-new').value.trim();
  const ok = await postAction('rename', {account, new_name:newName}, newName,
    account + ' 已改名为 ' + newName + '；旧名称的后续上传会自动归入新账号。',
    event.currentTarget.querySelector('button'));
  if (ok) $('#rename-new').value = '';
});
$('#merge-form').addEventListener('submit', async event => {
  event.preventDefault();
  const source = $('#merge-source').value, target = $('#merge-target').value;
  if (source === target) { status('请选择两个不同账号。', true); return; }
  await postAction('merge', {source,target}, target,
    source + ' 的历史已并入 ' + target + '；旧名称的后续上传也会归入保留账号。',
    event.currentTarget.querySelector('button'));
});
$('#delete-account').addEventListener('change', () => { $('#delete-confirm').value = ''; });
$('#delete-form').addEventListener('submit', async event => {
  event.preventDefault();
  const account = $('#delete-account').value;
  if ($('#delete-confirm').value !== account) { status('请完整输入账号名确认删除。', true); return; }
  const ok = await postAction('delete', {account, confirm_name:account}, '',
    account + ' 的记录已删除；同名自动上传已被拦截。',
    event.currentTarget.querySelector('button'));
  if (ok) $('#delete-confirm').value = '';
});
(async () => {
  try {
    const session = await api('api/session');
    if (!session.authenticated) {
      $('#management-login').hidden = false;
      status('请先返回看板登录。', true);
      return;
    }
    state.csrf = session.csrf_token;
    await loadAccounts();
    $('#management-content').hidden = false;
    status('');
  } catch (error) {
    if (error.status === 401) $('#management-login').hidden = false;
    status('读取账号失败：' + error.message, true);
  }
})();