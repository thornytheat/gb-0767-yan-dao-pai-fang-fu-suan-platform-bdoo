const form = document.querySelector('#monitor-form');
const sample = document.querySelector('#sample');
const feedback = document.querySelector('#feedback');
const locationFor = (name, run) => {
  const url = new URL(location.href);
  url.searchParams.set('sample', name);
  if (run) url.searchParams.set('run', run);
  else url.searchParams.delete('run');
  return url;
};
sample.addEventListener('change', () => location.assign(locationFor(sample.value)));
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  feedback.hidden = false;
  feedback.dataset.failed = 'false';
  feedback.textContent = '正在复算与归档';
  try {
    const response = await fetch('/api/runs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sample: sample.value}),
    });
    const record = await response.json();
    if (!response.ok) throw new Error(record.detail);
    const target = locationFor(sample.value, record.run_id);
    if (target.href === location.href) feedback.textContent = '已复用原归档';
    else location.assign(target);
  } catch (error) {
    feedback.dataset.failed = 'true';
    feedback.textContent = error.message || '归档失败，请重试';
  } finally {
    button.disabled = false;
  }
});
const filter = document.querySelector('#state-filter');
filter?.addEventListener('change', () => {
  for (const row of document.querySelectorAll('#verdict-table tbody tr')) {
    row.hidden = filter.value !== 'all' && (filter.value === 'late' ? row.dataset.late !== 'True' : row.dataset.state !== filter.value);
  }
});

const downloadJson = (payload, filename) => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
};

document.querySelectorAll('form.retention-form').forEach(retentionForm => {
  retentionForm.addEventListener('submit', async event => {
    event.preventDefault();
    const trigger = event.submitter;
    const issue = trigger?.name === 'issue' && trigger.value === '1';
    const runId = retentionForm.closest('tr')?.dataset.runId;
    if (!runId) return;
    const buttons = [...retentionForm.querySelectorAll('button')];
    buttons.forEach(button => { button.disabled = true; });
    feedback.hidden = false;
    feedback.dataset.failed = 'false';
    feedback.textContent = issue ? '正在签发证据并解除原件保留' : '正在解除原件保留';
    try {
      const response = await fetch(retentionForm.dataset.api || `/api/runs/${runId}/cleanup`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({issue}),
      });
      const record = await response.json();
      if (!response.ok) throw new Error(record.detail || '清理失败');
      if (record.certificate) downloadJson(record.certificate, `monitor-${runId}.json`);
      feedback.textContent = '原件保留已解除，正在刷新留档页';
      location.reload();
    } catch (error) {
      feedback.dataset.failed = 'true';
      feedback.textContent = error.message || '清理失败，请重试';
      buttons.forEach(button => { button.disabled = false; });
    }
  });
});

const verifyButton = document.querySelector('#verify-evidence');
verifyButton?.addEventListener('click', async () => {
  const fileInput = document.querySelector('#evidence-file');
  const textInput = document.querySelector('#evidence-text');
  const output = document.querySelector('#verify-output');
  if (!output) return;
  output.hidden = false;
  output.dataset.ok = 'false';
  try {
    let text = textInput?.value?.trim();
    if (fileInput?.files?.[0]) text = await fileInput.files[0].text();
    if (!text) throw new Error('请选择证据文件或粘贴证据 JSON');
    JSON.parse(text);
    const response = await fetch('/api/evidence/verify', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: text,
    });
    const record = await response.json();
    if (!response.ok) throw new Error(record.detail || record.code || '证据复核失败');
    output.dataset.ok = 'true';
    output.textContent = `证据有效：${record.certificate}\n运行编号：${record.run_id}\n分钟勾稽差额：${record.minute_balance}\n内容摘要：${record.sha256}`;
  } catch (error) {
    output.textContent = `复核失败：${error.message}`;
  }
});
