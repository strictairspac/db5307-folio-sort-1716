import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';
import {mountOrder} from './orders.js';

export function evidencePage(state, identity) {
  const current = state.evidence.find(row=>row.id===identity) || state.evidence[0];
  if (!current) return heading(topic.modules[3]) + '<p class="empty">执行完成后，可查看、复核和导出证据。</p>';
  const {report} = current;
  const metrics = Object.entries(report.metrics).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const sections = report.sections
    .filter(x=>x.title!=='上架顺序单')
    .map(x=>panel(x.title, entries(x.rows))).join('');
  const review = current.reviewer ? `<p data-testid="reviewed">${esc(current.reviewer)} · ${esc(date(current.reviewed_at))}</p><p>${esc(current.review_note)}</p>`
    : state.session.role==='reviewer'?'<form id="review-form"><label>复核意见<textarea name="note" maxlength="500" required></textarea></label><button>签署复核</button></form>'
    : '<p class="note">待复核员签署</p>';
  const history = state.evidence.map(row=>[`<a href="#evidence/${row.id}">执行 ${row.run_id}</a>`, esc(row.report.summary), esc(row.reviewer || '待复核')]);
  const key = `evidence:${current.id}`;
  const migrate = state.session.role==='operator'
    ? `<form id="migrate-form" class="actions"><label class="field"><span>每批迁移份数</span><input name="batch_size" type="number" min="1" max="200" step="1" value="10"></label><button class="secondary">迁移旧顺序单</button></form>`
    : '';
  return `${heading(topic.modules[3], report.summary, `<button id="download" data-id="${current.id}">导出证据</button>`)}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief"><h2>复核签署</h2>${review}<details><summary>结果校验码</summary><code class="digest">${esc(current.digest)}</code></details></aside></div>
    <section class="panel" id="order-slot" data-key="${esc(key)}"><h2>上架顺序单</h2><p class="note">正在读取顺序单……</p></section>
    ${migrate}${sections}<details><summary>历史证据</summary>${records(['执行','结论','复核'], history)}</details>`;
}

export function bindEvidence(api, update, fail) {
  const download = document.getElementById('download');
  const identity = download?.dataset.id;
  const current = download ? {id: Number(identity)} : null;
  download?.addEventListener('click', async()=>{
    try {
      const response = await api(`/evidence/${identity}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `evidence-${identity}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  });
  submit('review-form', async data=>{
    if (!current) return;
    await api(`/evidence/${current.id}/review`, {method:'POST', data:{note:data.get('note')}});
    await update();
  }, fail);
  submit('migrate-form', async data=>{
    const size = Math.min(200, Math.max(1, Number(data.get('batch_size')) || 10));
    await api(`/orders/migrate?batch_size=${size}`, {method:'POST'});
    await update();
  }, fail);
  const slot = document.getElementById('order-slot');
  if (slot) void mountOrder(api, slot.dataset.key, slot, fail);
}
