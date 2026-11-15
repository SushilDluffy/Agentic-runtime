const element = (id) => document.getElementById(id);
let taskId = localStorage.getItem('supportTask');
let task;
let stream;
let cursor = 0;
const terminal = new Set(['succeeded', 'failed', 'cancelled']);
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
function report(error) { element('error').textContent = error.message; }
async function refresh() {
  task = await api(`/tasks/${taskId}`);
  element('status').textContent = task.status.replaceAll('_', ' ');
  element('cancel').disabled = terminal.has(task.status);
  element('start').disabled = !terminal.has(task.status) && task.status !== 'blocked';
  element('resume').hidden = task.status !== 'blocked';
  element('result').textContent = JSON.stringify(await api(`/tasks/${taskId}/result`), null, 2);
  const pending = task.pending;
  element('interaction').hidden = !pending;
  if (pending) {
    const approval = pending.kind === 'approval';
    element('prompt').textContent = pending.prompt;
    const operation = task.operations.find((op) => op.id === pending.operation_id);
    element('payload').textContent = JSON.stringify(approval ? {
      action: operation.action, arguments: operation.arguments
    } : pending.schema, null, 2);
    element('reply').hidden = approval;
    element('reply-label').hidden = approval;
    if (!approval && !element('reply').value) element('reply').value = JSON.stringify({resolution: 'Your invoice download link has been refreshed.'}, null, 2);
    element('respond').textContent = approval ? 'Approve and send' : 'Submit reply';
    element('reject').hidden = !approval;
  }
}
function listen() {
  if (stream) stream.close();
  stream = new EventSource(`/tasks/${taskId}/events?after=${cursor}`);
  stream.addEventListener('progress', (event) => {
    cursor = Number(event.lastEventId);
    const data = JSON.parse(event.data);
    const item = document.createElement('li');
    item.textContent = `${data.kind.replaceAll('.', ' ')} · ${new Date(data.created_at * 1000).toLocaleTimeString()}`;
    element('events').append(item);
  });
  stream.addEventListener('idle', () => { stream.close(); refresh().catch(report); });
  stream.onerror = () => { refresh().catch(report); };
  refresh().catch(report);
}
async function answer(value) {
  element('error').textContent = '';
  await api(`/tasks/${taskId}/requests/${task.pending.id}`, {
    value, input_digest: task.pending.input_digest
  });
  element('interaction').hidden = true;
  listen();
}
element('start').onclick = async () => {
  try {
    element('error').textContent = '';
    element('start').disabled = true;
    task = await api('/tasks', {agent: 'support', objective: element('objective').value});
    taskId = task.id; cursor = 0;
    localStorage.setItem('supportTask', taskId);
    element('events').replaceChildren();
    listen();
  } catch (error) { element('start').disabled = false; report(error); }
};
element('cancel').onclick = () => api(`/tasks/${taskId}/cancel`, {}).then(() => { listen(); }).catch(report);
element('resume').onclick = () => api(`/tasks/${taskId}/resume`, {}).then(() => { listen(); }).catch(report);
element('respond').onclick = () => {
  try { answer(task.pending.kind === 'approval' ? true : JSON.parse(element('reply').value)).catch(report); }
  catch (error) { report(error); }
};
element('reject').onclick = () => answer(false).catch(report);
if (taskId) listen();
