'use strict';
const $ = (id) => document.getElementById(id);
let config = null, current = null, sending = false, stopRequested = false, budgetVersion = 0;
let examSnapshotAt = Date.now(), budgetTimeout, toastTimeout;
const headers = {'Content-Type': 'application/json', 'X-Local-Client': 'olpai'};

async function api(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Yêu cầu không hợp lệ.');
  return data;
}
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimeout); toastTimeout = setTimeout(() => $('toast').hidden = true, 6500);
}
function formatNumber(value) {return Number(value).toLocaleString('vi-VN');}
function showContext(prompt, output = 0, dropped = 0) {
  const limit = config?.context_limit || 2048;
  $('context-used').textContent = `${formatNumber(prompt)} / ${formatNumber(limit)}`;
  $('context-meter').max = limit; $('context-meter').value = Math.min(limit, prompt);
  $('budget-detail').textContent = output ? `Còn tối đa ${output} token sinh${dropped ? ` · bỏ ${dropped} tin cũ` : ''}` : 'Gồm lịch sử, câu hỏi và phần sinh';
}
function clock(value) {
  const s = Math.max(0, Math.ceil(value));
  return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map(n => String(n).padStart(2, '0')).join(':');
}
function updateControls() {
  const exam = config?.exam;
  const delta = (Date.now() - examSnapshotAt) / 1000;
  const locked = exam && exam.ai_remaining - delta <= 0;
  const oldExam = current?.exam_id && (!exam || current.exam_id !== exam.id);
  const otherSession = exam && current && current.exam_id !== exam.id;
  $('send').disabled = sending || !!locked || !!oldExam || !!otherSession || !!current?.blocked || !config?.tokenizer_ready;
  $('prompt').disabled = sending || !!locked || !!oldExam || !!otherSession || !!current?.blocked;
  $('new-session').disabled = sending || !!locked;
  $('policy').disabled = sending || !!exam;
  $('start-exam').disabled = sending;
  $('export').disabled = !current;
  $('stop').hidden = !sending;
  $('send').hidden = sending;
  $('composer-hint').textContent = locked ? 'AI đã khóa. Bạn tiếp tục làm bài bằng notebook.' : oldExam || otherSession ? 'Phiên này chỉ đọc trong trạng thái thi hiện tại.' : current?.blocked || 'Enter để gửi · Shift + Enter xuống dòng';
  $('exam-clock').hidden = !exam;
  $('start-exam').hidden = !!exam;
  $('end-exam').hidden = !exam;
  if (exam) {
    const remaining = Math.max(0, exam.exam_remaining - delta);
    $('exam-clock').textContent = clock(remaining);
    $('exam-description').textContent = remaining <= 0 ? 'Đã hết 6 giờ · kết thúc bài để bắt đầu lượt khác' : locked ? 'Giờ cuối · AI đã khóa, tự tạo kết quả trên private test' : `AI còn ${clock(exam.ai_remaining - delta)} · tạo phiên mới không đặt lại giờ`;
  } else $('exam-description').textContent = '6 giờ làm bài · AI hỗ trợ trong 5 giờ đầu';
}
async function loadStatus() {
  config = await api('/api/status'); examSnapshotAt = Date.now();
  const notes = [];
  if (config.demo) notes.push('CHẾ ĐỘ DEMO — câu trả lời giả, chưa gọi DeepSeek. Lịch sử demo được lưu riêng.');
  if (!config.tokenizer_ready) notes.push(config.tokenizer_error);
  if (!config.demo && config.configuration_error) notes.push(config.configuration_error + ' Không gửi token vào chat.');
  if (!config.demo && config.availability.checked_at && !config.availability.available) notes.push(config.availability.error || `${config.provider_label} hiện chưa có endpoint cho đúng model 32B. App sẽ không tự đổi model.`);
  $('provider-label').textContent = config.demo ? 'Demo · chưa gọi model' : config.provider_label;
  $('setup-notice').textContent = notes.join('\n'); $('setup-notice').hidden = !notes.length;
  const badge = $('connection');
  badge.textContent = config.demo ? 'Demo · chưa gọi model' : config.availability.available ? 'Đã kiểm tra danh mục model' : config.availability.checked_at ? 'Chưa có kết nối' : 'Chưa kiểm tra endpoint';
  badge.className = 'badge ' + (config.demo || !config.availability.available ? 'warn' : '');
  updateControls();
}
async function listSessions() {
  const sessions = await api('/api/sessions'); $('sessions').replaceChildren();
  for (const session of sessions) {
    const button = document.createElement('button'); button.className = 'session-button' + (current?.id === session.id ? ' active' : '');
    button.textContent = (session.exam_id ? '◷ ' : '↳ ') + session.title; button.title = session.title;
    button.disabled = sending; button.onclick = () => selectSession(session.id).catch(e => toast(e.message));
    $('sessions').append(button);
  }
  return sessions;
}
function renderText(container, text) {
  container.replaceChildren();
  // No innerHTML, remote images, link previews, scripts, or clickable external URLs.
  const chunks = text.split('```');
  chunks.forEach((chunk, index) => {
    if (index % 2 === 0) {
      if (!chunk) return;
      const p = document.createElement('div'); p.className = 'paragraph';
      // A small, safe inline formatter; URLs/HTML remain plain text.
      const fragments = chunk.split(/(\*\*[^\n*]+\*\*|`[^\n`]+`)/g);
      for (const fragment of fragments) {
        if (fragment.startsWith('**') && fragment.endsWith('**')) {
          const strong = document.createElement('strong'); strong.textContent = fragment.slice(2, -2); p.append(strong);
        } else if (fragment.startsWith('`') && fragment.endsWith('`')) {
          const code = document.createElement('code'); code.textContent = fragment.slice(1, -1); p.append(code);
        } else p.append(document.createTextNode(fragment));
      }
      container.append(p);
    } else {
      const newline = chunk.indexOf('\n');
      const language = newline >= 0 ? chunk.slice(0, newline).trim() : '';
      const code = newline >= 0 ? chunk.slice(newline + 1).replace(/\n$/, '') : chunk;
      const block = document.createElement('div'); block.className = 'code-block';
      const heading = document.createElement('div'); heading.className = 'code-heading';
      const label = document.createElement('span'); label.textContent = language || 'code';
      const copy = document.createElement('button'); copy.textContent = 'Copy code'; copy.onclick = () => copyText(code);
      const pre = document.createElement('pre'); const codeElement = document.createElement('code'); codeElement.textContent = code;
      pre.append(codeElement); heading.append(label, copy); block.append(heading, pre); container.append(block);
    }
  });
}
async function copyText(text) {
  try {await navigator.clipboard.writeText(text); toast('Đã copy.');} catch {toast('Không copy được. Bạn có thể chọn và copy văn bản thủ công.');}
}
function messageElement(message) {
  const article = document.createElement('article'); article.className = `message ${message.role}` + (!message.active && message.status === 'complete' ? ' inactive' : '');
  const head = document.createElement('div'); head.className = 'message-head';
  const avatar = document.createElement('span'); avatar.className = 'mini-avatar'; avatar.textContent = message.role === 'user' ? 'BẠN' : 'DS';
  const name = document.createElement('span'); name.textContent = message.role === 'user' ? 'Bạn' : config?.demo ? 'Demo · câu trả lời giả' : 'DeepSeek R1 · 32B';
  head.append(avatar, name);
  if (message.status !== 'complete' || !message.active) {
    const tag = document.createElement('span'); tag.className = 'message-tag';
    tag.textContent = message.status === 'complete' ? 'Ngoài context hiện tại' : message.status === 'pending' ? 'Đang sinh' : 'Bản nháp · chưa hoàn tất'; head.append(tag);
  }
  const reasoning = document.createElement('details'); reasoning.className = 'reasoning'; reasoning.hidden = !message.reasoning;
  const summary = document.createElement('summary'); summary.textContent = 'Phần reasoning'; const pre = document.createElement('pre'); pre.textContent = message.reasoning || '';
  reasoning.append(summary, pre);
  const body = document.createElement('div'); body.className = 'message-text'; renderText(body, message.content || '');
  article.append(head, reasoning, body);
  if (message.role === 'assistant') {
    const tools = document.createElement('div'); tools.className = 'message-tools';
    const copy = document.createElement('button'); copy.textContent = 'Copy câu trả lời'; copy.onclick = () => copyText(message.content || '');
    tools.append(copy); article.append(tools);
  }
  return {article, body, reasoning, reasoningText: pre};
}
async function selectSession(id) {
  if (sending) return;
  current = await api(`/api/sessions/${id}`);
  localStorage.setItem('olpai-session', id); $('policy').value = current.policy;
  $('messages').replaceChildren(); $('welcome').hidden = current.messages.length > 0;
  for (const message of current.messages) $('messages').append(messageElement(message).article);
  showContext(current.context_tokens || 0); updateControls(); await listSessions();
  if (current.running) toast('Phiên đang sinh ở tab khác. Tải lại sau khi hoàn tất.');
  for (const generation of current.generations.slice(-1)) {
    if (generation.finish_reason === 'length') toast('Câu trả lời đã chạm giới hạn sinh; code có thể chưa hoàn chỉnh.');
    const usage = generation.usage ? JSON.parse(generation.usage) : null;
    if (usage) $('usage-note').textContent = `${usage.total_tokens ?? (usage.prompt_tokens + usage.completion_tokens)} token lượt cuối` + (typeof usage.cost === 'number' ? ` · $${usage.cost.toFixed(5)}` : '');
  }
}
async function createSession() {
  const session = await api('/api/sessions', 'POST', {policy: $('policy').value});
  $('prompt').value = ''; await selectSession(session.id); return session;
}
async function previewBudget() {
  const version = ++budgetVersion;
  const text = $('prompt').value;
  if (!current || !text.trim() || sending || !config?.tokenizer_ready) {
    if (!sending) showContext(current?.context_tokens || 0);
    return;
  }
  try {
    const budget = await api(`/api/sessions/${current.id}/budget`, 'POST', {text});
    if (version !== budgetVersion || sending) return;
    if (budget.fits) showContext(budget.prompt_tokens, budget.max_tokens, budget.dropped_messages);
    else $('budget-detail').textContent = 'Không đủ context · rút ngắn hoặc tạo phiên mới';
  } catch (e) {if (version === budgetVersion) $('budget-detail').textContent = e.message;}
}
function scrollBottom() {$('chat').scrollTop = $('chat').scrollHeight;}
async function sendMessage(event) {
  event.preventDefault(); if (sending || $('send').disabled) return;
  const text = $('prompt').value.trim(); if (!text) return;
  if (!current) {try {await createSession();} catch (e) {toast(e.message); return;}}
  sending = true; stopRequested = false; ++budgetVersion; updateControls();
  const node = messageElement({role: 'assistant', content: '', status: 'pending', active: true});
  let content = '', reasoning = '', gotStart = false, gotDone = false;
  try {
    await listSessions();
    const response = await fetch(`/api/sessions/${current.id}/chat`, {method: 'POST', headers, body: JSON.stringify({text})});
    if (!response.ok) {
      const error = await response.json(); throw new Error(error.detail || 'Không gửi được câu hỏi.');
    }
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const result = await reader.read(); buffer += decoder.decode(result.value || new Uint8Array(), {stream: !result.done});
      let end;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const name = raw.split('\n').find(line => line.startsWith('event: '))?.slice(7);
        const dataLine = raw.split('\n').find(line => line.startsWith('data: ')); if (!dataLine) continue;
        const data = JSON.parse(dataLine.slice(6));
        if (name === 'start') {
          gotStart = true; $('welcome').hidden = true;
          $('messages').append(messageElement({role: 'user', content: text, status: 'complete', active: true}).article, node.article);
          node.body.classList.add('streaming'); $('prompt').value = '';
          showContext(data.prompt_tokens, data.max_tokens, data.dropped_messages); scrollBottom();
          if (stopRequested) await api(`/api/sessions/${current.id}/stop`, 'POST', {});
        } else if (name === 'delta') {content += data.text; renderText(node.body, content); scrollBottom();}
        else if (name === 'reasoning') {reasoning += data.text; node.reasoning.hidden = false; node.reasoningText.textContent = reasoning;}
        else if (name === 'error' || name === 'warning') toast(data.message);
        else if (name === 'done') {gotDone = true; if (data.finish_reason === 'length') toast('Đã chạm giới hạn sinh. Kiểm tra code trước khi chạy.');}
      }
      if (result.done) break;
    }
    if (!gotDone) toast('Kết nối đã đóng. Kiểm tra bản nháp; app không tự gọi lại.');
  } catch (e) {toast(e.message);}
  finally {
    sending = false; node.body.classList.remove('streaming');
    try {await loadStatus(); await selectSession(current.id);
      if (gotStart) scrollBottom(); else previewBudget();
    } catch (e) {updateControls(); toast(e.message);}
  }
}
function handle(action) {return async () => {try {await action();} catch (e) {toast(e.message);}};}
$('new-session').onclick = handle(createSession);
$('composer').onsubmit = sendMessage;
$('prompt').addEventListener('keydown', (event) => {if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); $('composer').requestSubmit();}});
$('prompt').addEventListener('input', () => {clearTimeout(budgetTimeout); budgetTimeout = setTimeout(previewBudget, 180);});
$('stop').onclick = handle(async () => {stopRequested = true; await api(`/api/sessions/${current.id}/stop`, 'POST', {});});
$('check-connection').onclick = handle(async () => {
  $('check-connection').disabled = true;
  try {await api('/api/availability', 'POST', {}); await loadStatus();} finally {$('check-connection').disabled = false;}
});
$('export').onclick = () => {if (current) window.location.assign(`/api/sessions/${current.id}/export`);};
document.querySelectorAll('[data-prompt]').forEach(button => button.onclick = () => {$('prompt').value = button.dataset.prompt; $('prompt').focus(); previewBudget();});
$('start-exam').onclick = () => $('exam-dialog').showModal();
$('cancel-exam').onclick = () => $('exam-dialog').close();
$('confirm-exam').onclick = handle(async () => {await api('/api/exams', 'POST', {policy: $('policy').value}); $('exam-dialog').close(); await loadStatus(); await createSession();});
$('end-exam').onclick = () => $('end-dialog').showModal();
$('cancel-end').onclick = () => $('end-dialog').close();
$('confirm-end').onclick = handle(async () => {await api('/api/exams/end', 'POST', {}); $('end-dialog').close(); await loadStatus(); await createSession();});
async function init() {
  await loadStatus(); const sessions = await listSessions();
  const remembered = localStorage.getItem('olpai-session');
  const selected = sessions.find(s => s.id === remembered) || sessions.find(s => config.exam ? s.exam_id === config.exam.id : !s.exam_id);
  if (selected) await selectSession(selected.id);
  else if (!config.exam?.ai_locked) await createSession();
  setInterval(updateControls, 1000);
  setInterval(() => {if (!sending) loadStatus().catch(() => {});}, 15000);
  await api('/api/availability', 'POST', {}); await loadStatus();
}
init().catch(e => toast('Không khởi tạo được app: ' + e.message));
