import {records} from '../components/common/records.js';
import {el, esc} from '../utils/dom.js';

// 顺序单分页读取：每次只请求覆盖当前页的段。翻页凭据由服务端签发，
// 携带稳定键、偏移与整单校验码，迁移切换前后都解析到同一份顺序单。
export function orderPanel(key, page) {
  const rows = page.rows.map(row=>[
    esc(row.上架顺位), esc(row.馆藏件号), esc(row.馆藏登录号), esc(row.题名), esc(row.原登记顺位)]);
  const hint = page.segments_read.length
    ? `本页读取段 ${page.segments_read.map(x=>`${x.ordinal}（${x.start_rank}–${x.end_rank}）`).join('、')}`
    : '旧档尚未分段，本页直接读取旧证据';
  return `<h2>上架顺序单</h2>
    <div class="spread"><div>${records(['顺位','件号','登录号','题名','原登记'], rows, '当前页没有馆藏件。')}</div>
    <aside class="brief"><dl>
      <dt>整单件数</dt><dd data-testid="order-total">${esc(page.total)}</dd>
      <dt>存档段数</dt><dd data-testid="order-segments">${page.segments===null?'迁移前旧档':esc(page.segments)}</dd>
      <dt>每段件数</dt><dd>${page.segment_items===null?'—':esc(page.segment_items)}</dd>
      <dt>当前页</dt><dd>${page.total ? `${esc(page.start_rank)}–${esc(page.end_rank)}` : '空单'}</dd>
    </dl><p class="note" data-testid="segments-read">${esc(hint)}</p>
    <p class="note">${page.source==='archive'?'分段存档 · 整单可校验':'旧证据兼容读取'}</p>
    <details><summary>整单校验码</summary><code class="digest">${esc(page.checksum)}</code></details></aside></div>
    <div class="actions">
      <button id="order-prev" class="secondary" type="button" ${page.prev_cursor?'':'disabled'}>上一页</button>
      <button id="order-next" class="secondary" type="button" ${page.next_cursor?'':'disabled'}>下一页</button>
      <button id="order-download" type="button">导出顺序单</button>
    </div>`;
}

export async function mountOrder(api, key, slot, fail) {
  const draw = page=>{
    slot.innerHTML = orderPanel(key, page);
    document.getElementById('order-prev')?.addEventListener('click', ()=>{
      if (page.prev_cursor) void load(page.prev_cursor);
    });
    document.getElementById('order-next')?.addEventListener('click', ()=>{
      if (page.next_cursor) void load(page.next_cursor);
    });
    document.getElementById('order-download')?.addEventListener('click', async()=>{
      try {
        const response = await api(`/orders/${encodeURIComponent(key)}/export`, {response:true});
        const url = URL.createObjectURL(await response.blob());
        const link = el('a');
        link.href = url;
        link.download = `order-${key.replace(/[^0-9A-Za-z_-]/g,'_')}.jsonl`;
        link.click();
        setTimeout(()=>URL.revokeObjectURL(url), 1000);
      } catch (error) { fail(error); }
    });
  };
  const load = async cursor=>{
    try {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '?size=20';
      draw(await api(`/orders/${encodeURIComponent(key)}/page${query}`));
    } catch (error) { fail(error); }
  };
  await load(null);
}
