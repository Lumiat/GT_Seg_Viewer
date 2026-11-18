'use strict';
const $ = id => document.getElementById(id);
const state = {tag:'1v0', page:1, pages:1, ticket:0, cards:[], detail:null, opacity:.48, mode:'both'};
const descriptions = {
 '1v0':['未关联 S0 的 GT','每张卡片对应一个 GT，默认显示 GT 面积最大的切片。'],
 '1v1':['一对一的 GT / S0','默认定位二者共同出现的切片，在同一张 EM 上对照。'],
 '1vn':['关联多个对象的 GT','优先展示最多关联 S0；通过代表切片与切片控件浏览其余 S0。']
};
async function json(url, signal) {
 const response=await fetch(url,{signal});
 if(!response.ok) throw Error(await response.text());
 return response.json();
}
function showError(message){$('error').textContent=message;$('error').hidden=!message;}
function decodeFrame(buffer){
 const v=new DataView(buffer);
 if(new TextDecoder().decode(buffer.slice(0,4))!=='GSV1')throw Error('切片格式无效');
 const w=v.getUint32(4,true),h=v.getUint32(8,true),n=v.getUint32(12,true),size=w*h,offset=16+n;
 if(buffer.byteLength!==offset+size*6)throw Error('切片数据不完整');
 const meta=JSON.parse(new TextDecoder().decode(buffer.slice(16,offset)));
 return {w,h,meta,em:new Uint8Array(buffer,offset,size),gt:new Uint8Array(buffer,offset+size,size),s0:new Uint32Array(buffer.slice(offset+size*2))};
}
function boundary(a,i,w,h){
 const x=i%w,y=Math.floor(i/w),s=a[i];
 return s&&(x===0||y===0||x===w-1||y===h-1||a[i-1]!==s||a[i+1]!==s||a[i-w]!==s||a[i+w]!==s);
}
function paint(card){
 const p=card.frame;if(!p)return;
 const surface=document.createElement('canvas');surface.width=p.w;surface.height=p.h;
 const ctx=surface.getContext('2d'),img=ctx.createImageData(p.w,p.h),out=img.data;
 for(let i=0;i<p.em.length;i++){
  let colour=null,alpha=state.opacity;
  if((state.mode==='both'||state.mode==='s0')&&p.s0[i])colour=p.meta.s0_colours[p.s0[i]];
  if(state.mode==='gt'&&p.gt[i])colour=p.meta.gt_colour;
  if(state.mode==='both'&&p.gt[i]){
   if(boundary(p.gt,i,p.w,p.h)){colour=p.meta.gt_colour;alpha=Math.min(1,state.opacity*1.8);}
   else if(!p.s0[i]){colour=p.meta.gt_colour;alpha=state.opacity*.3;}
  }
  for(let c=0;c<3;c++)out[i*4+c]=colour?p.em[i]*(1-alpha)+colour[c]*alpha:p.em[i];
  out[i*4+3]=255;
 }
 ctx.putImageData(img,0,0);card.surface=surface;fitCanvas(card);
}
function viewTransform(card,width,height){
 if(card.focusBounds){
  const [x0,y0,x1,y1]=card.focusBounds;
  const base=Math.min(width/card.surface.width,height/card.surface.height);
  // Fit the complete object (including disconnected parts) with 10% padding per side.
  card.zoom=Math.max(1,Math.min(width/((x1-x0)*1.2),height/((y1-y0)*1.2))/base);
  card.maxZoom=Math.max(16,card.zoom);
  card.cx=(x0+x1)/2;card.cy=(y0+y1)/2;
 }
 const s=Math.min(width/card.surface.width,height/card.surface.height)*card.zoom;
 // Focused objects near crop edges must still be centred; allow background margins.
 if(!card.freeCenter){
  const hx=Math.min(card.surface.width/2,width/(2*s)),hy=Math.min(card.surface.height/2,height/(2*s));
  card.cx=Math.max(hx,Math.min(card.surface.width-hx,card.cx));
  card.cy=Math.max(hy,Math.min(card.surface.height-hy,card.cy));
 }
 return {s,x:width/2-card.cx*s,y:height/2-card.cy*s};
}
function focusS0(card,id){
 const p=card.frame,slot=p.meta.s0_ids.indexOf(id);if(slot<=0)return;
 let x0=p.w,y0=p.h,x1=0,y1=0;
 for(let i=0;i<p.s0.length;i++)if(p.s0[i]===slot){
  const x=i%p.w,y=Math.floor(i/p.w);
  x0=Math.min(x0,x);y0=Math.min(y0,y);x1=Math.max(x1,x+1);y1=Math.max(y1,y+1);
 }
 if(x1<=x0||y1<=y0)return;
 card.focusBounds=[x0,y0,x1,y1];card.focusId=id;card.freeCenter=true;
}
function resetZoom(card){
 card.focusBounds=null;card.focusId=null;card.requestedFocusId=null;card.freeCenter=false;card.maxZoom=16;
 card.zoom=1;card.cx=(card.surface?.width||0)/2;card.cy=(card.surface?.height||0)/2;
 fitCanvas(card);
}
function zoomAt(card,event){
 if(!card.surface||!event.deltaY)return;
 const rect=card.canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
 const old=viewTransform(card,rect.width,rect.height),x=event.clientX-rect.left,y=event.clientY-rect.top;
 const px=(x-old.x)/old.s,py=(y-old.y)/old.s;
 const delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?rect.height:1);
 const zoom=Math.max(1,Math.min(card.maxZoom||16,card.zoom*Math.exp(-Math.max(-120,Math.min(120,delta))*.002)));
 if(zoom===card.zoom)return;
 card.focusBounds=null;
 const s=old.s*zoom/card.zoom;card.zoom=zoom;
 card.cx=px-(x-rect.width/2)/s;card.cy=py-(y-rect.height/2)/s;
 fitCanvas(card);
}
function fitCanvas(card){
 if(!card.surface)return;
 const canvas=card.canvas,rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
 canvas.width=Math.max(1,Math.round(rect.width*dpr));canvas.height=Math.max(1,Math.round(rect.height*dpr));
 const ctx=canvas.getContext('2d'),{s,x,y}=viewTransform(card,canvas.width,canvas.height);
 ctx.fillStyle='#171d26';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.imageSmoothingEnabled=false;
 ctx.drawImage(card.surface,x,y,card.surface.width*s,card.surface.height*s);
}
function swatch(text,colour,outline=false){
 const span=document.createElement('span'),i=document.createElement('i');i.className='swatch';
 if(outline)i.style.border=`2px solid rgb(${colour})`;else i.style.background=`rgb(${colour})`;
 span.append(i,document.createTextNode(text));return span;
}
function controls(card){
 const {root,z,zs,row}=card,index=zs.indexOf(z);
 root.querySelector('.z-badge').textContent=z==null?'GT 缺失':`Z = ${z}`;
 const range=root.querySelector('.slice-range');range.max=Math.max(0,zs.length-1);range.value=Math.max(0,index);range.disabled=zs.length<2;
 root.querySelector('.prev-slice').disabled=!zs.length||index===0;
 root.querySelector('.next-slice').disabled=!zs.length||index===zs.length-1;
 root.querySelector('.slice-counter').textContent=index<0?'单独定位':`${index+1} / ${zs.length}`;
 root.querySelector('.reset-slice').disabled=row.initial_z==null;
 for(const button of root.querySelectorAll('.recommendations button'))button.setAttribute('aria-pressed',String(Number(button.dataset.z)===z));
}
function metadata(card){
 const {root,row,frame:p}=card;
 const present=new Set(p.meta.present_s0),n=row.covered_s0_id.length;
 root.querySelector('.coverage').textContent=row.tag==='1v0'?'仅 GT · 无关联 S0':`当前可见 S0：${present.size} / ${n}`+(p.meta.gt_present?' · GT 在场':' · GT 不在此切片');
 const legend=root.querySelector('.legend');legend.replaceChildren(swatch(p.meta.gt_present?'GT':'GT 不在场',p.meta.gt_colour,true));
 for(const id of p.meta.present_s0.slice(0,3)){const i=p.meta.s0_ids.indexOf(id);legend.append(swatch(`S0 ${id}`,p.meta.s0_colours[i]));}
 if(p.meta.present_s0.length>3){const more=document.createElement('span');more.textContent=`+${p.meta.present_s0.length-3} 个`;legend.append(more);}
 for(const button of root.querySelectorAll('.s0-list button'))button.classList.toggle('present',present.has(button.dataset.id));
}
async function loadFrame(card,z,focusId=null){
 if(z==null)return;
 card.controller?.abort();card.controller=new AbortController();const ticket=++card.ticket;
 card.z=z;card.requestedFocusId=focusId;controls(card);const message=card.root.querySelector('.image-message');message.textContent='正在加载切片…';message.classList.remove('failed');
 try{
  const response=await fetch(`/api/frame/${card.row.gt_id}/${z}`,{signal:card.controller.signal});
  if(!response.ok)throw Error(await response.text());const p=decodeFrame(await response.arrayBuffer());
  if(ticket!==card.ticket||!card.root.isConnected)return;
  // Each Z has its own crop; fit the new crop instead of reusing old coordinates.
  card.zoom=1;card.cx=p.w/2;card.cy=p.h/2;
  card.focusBounds=null;card.focusId=null;card.freeCenter=false;card.maxZoom=16;
  card.frame=p;if(focusId!==null)focusS0(card,focusId);
  paint(card);metadata(card);message.textContent='';
 }catch(error){if(error.name!=='AbortError'&&ticket===card.ticket){message.textContent='加载失败，点击重试：'+error.message;message.classList.add('failed');}}
}
function step(card,delta){
 if(!card.zs.length)return;const i=card.zs.indexOf(card.z),next=Math.max(0,Math.min(card.zs.length-1,i+delta));
 if(card.zs[next]!==card.z)loadFrame(card,card.zs[next]);
}
function makeCard(row,container,isDetail=false,startZ=null,startFocusId=null){
 const root=$('card-template').content.firstElementChild.cloneNode(true),canvas=root.querySelector('canvas');
 const card={root,canvas,row,z:startZ??row.initial_z,zs:row.slices.map(s=>s.z),ticket:0,frame:null,surface:null,controller:null,zoom:1,cx:0,cy:0};
 root.querySelector('h3').textContent=`GT ${row.gt_id}`;
 root.querySelector('.tag-pill').textContent=row.tag==='1v0'?'1v0':`${row.tag} · ${row.covered_s0_id.length} S0`;
 canvas.setAttribute('aria-label',`GT ${row.gt_id} EM 图，滚轮缩放，方向键换切片，F 适应画面${isDetail?'':'，Enter 查看大图'}`);
 const warnings=[];
 if(row.gt_missing)warnings.push('源数据中找不到该 GT。');
 if(row.missing_s0.length)warnings.push(`缺失 S0：${row.missing_s0.join('、')}。`);
 if(row.disjoint_s0.length)warnings.push(`无共同 Z 的 S0：${row.disjoint_s0.join('、')}；可在下方列表单独定位。`);
 if(row.s0_count_per_gt!==row.covered_s0_id.length)warnings.push(`CSV 计数为 ${row.s0_count_per_gt}，实际列表含 ${row.covered_s0_id.length} 个；按列表显示。`);
 if(row.tag!=='1v0'&&!row.all_together&&!row.gt_missing)warnings.push('无法让全部 S0 与 GT 同处一个 Z，请浏览代表切片。');
 const warning=root.querySelector('.warning');warning.textContent=warnings.join(' ');warning.hidden=!warnings.length;
 const recommendations=root.querySelector('.recommendations');
 for(const z of row.recommended_z){const b=document.createElement('button');b.textContent=`Z ${z}`;b.dataset.z=z;b.title=`代表切片 Z=${z}`;b.onclick=()=>loadFrame(card,z);recommendations.append(b);}
 root.querySelector('.s0-details summary').textContent=`关联 S0 列表 (${row.covered_s0_id.length}) · 点击居中放大`;
 root.querySelector('.s0-details').hidden=!row.covered_s0_id.length;
 for(const id of row.covered_s0_id){
  const b=document.createElement('button');b.dataset.id=id;b.textContent=id;b.disabled=row.s0_locations[id]==null;
  const joint=row.slices.filter(s=>s.s0_ids.includes(id));
  b.title=b.disabled?'源数据中缺失':joint.length?'切换到共存切片，将此 S0 完整放大并居中':'将此 S0 完整放大并居中（GT 不在场）';
  b.onclick=()=>loadFrame(card,(joint.find(s=>row.recommended_z.includes(s.z))||joint[0])?.z??row.s0_locations[id],id);
  root.querySelector('.s0-list').append(b);
 }
 const scope=root.querySelector('.slice-scope');
 if(row.tag==='1v0'||!row.slices.some(s=>s.s0_ids.length)){scope.value='gt';scope.options[0].disabled=true;card.zs=row.gt_slices;}
 scope.onchange=()=>{card.zs=scope.value==='gt'?row.gt_slices:row.slices.map(s=>s.z);if(!card.zs.includes(card.z)&&card.zs.length)loadFrame(card,card.zs[0]);else controls(card);};
 root.querySelector('.prev-slice').onclick=()=>step(card,-1);root.querySelector('.next-slice').onclick=()=>step(card,1);
 root.querySelector('.slice-range').oninput=e=>loadFrame(card,card.zs[Number(e.target.value)]);
 root.querySelector('.reset-slice').onclick=()=>loadFrame(card,row.initial_z);
 root.querySelector('.fit-image').onclick=()=>resetZoom(card);
 root.querySelector('.image-message').onclick=()=>loadFrame(card,card.z,card.requestedFocusId);
 canvas.addEventListener('wheel',e=>{e.preventDefault();zoomAt(card,e);},{passive:false});
 canvas.onkeydown=e=>{if(['ArrowDown','ArrowRight','ArrowUp','ArrowLeft'].includes(e.key)){e.preventDefault();step(card,['ArrowDown','ArrowRight'].includes(e.key)?1:-1);}else if(e.key.toLowerCase()==='f'){e.preventDefault();resetZoom(card);}else if(!isDetail&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openDetail(card);}};
 if(!isDetail)canvas.onclick=()=>openDetail(card);
 container.append(root);controls(card);
 if(card.z!=null)loadFrame(card,card.z,startFocusId);else{root.querySelector('.image-message').textContent='GT 不在源数据中';root.querySelector('.coverage').textContent='请核对 CSV 与源数据版本';}
 return card;
}
function closeDetail(){if($('detail').open)$('detail').close();state.detail?.controller?.abort();state.detail=null;$('detail-body').replaceChildren();}
function openDetail(card){closeDetail();$('detail').showModal();state.detail=makeCard(card.row,$('detail-body'),true,card.z,card.requestedFocusId??card.focusId);}
function pagination(){
 $('page-number').value=state.page;$('page-number').max=state.pages;$('pages').textContent=state.pages;
 $('prev-page').disabled=state.page<=1;$('next-page').disabled=state.page>=state.pages;
}
async function loadPage(){
 const ticket=++state.ticket;closeDetail();state.pageController?.abort();state.pageController=new AbortController();
 const signal=state.pageController.signal;
 for(const card of state.cards)card.controller?.abort();state.cards=[];$('gallery').replaceChildren();$('empty').hidden=true;showError('');
 $('prev-page').disabled=true;$('next-page').disabled=true;$('result-count').textContent='';
 for(const button of document.querySelectorAll('[data-tag]'))button.setAttribute('aria-pressed',String(button.dataset.tag===state.tag));
 [$('heading').textContent,$('description').textContent]=descriptions[state.tag];$('status').textContent='正在定位 GT 与关联 S0…';
 try{
  for(;;){
   const data=await json(`/api/clusters?tag=${state.tag}&page=${state.page}`,signal);if(ticket!==state.ticket)return;
   if(data.status==='building'){$('status').textContent=`首次建立切片索引：${data.done} / ${data.total}。完成后自动显示图集。`;await new Promise(r=>setTimeout(r,700));if(ticket!==state.ticket)return;continue;}
   if(data.status!=='ready')throw Error(data.error||'索引失败');
   state.pages=data.pages;pagination();$('status').textContent='';$('empty').hidden=!!data.total;
   $('result-count').textContent=data.total?`${(state.page-1)*8+1}–${Math.min(state.page*8,data.total)} / ${data.total} 个 GT`:'0 个 GT';
   state.cards=data.items.map(row=>makeCard(row,$('gallery')));return;
  }
 }catch(error){if(error.name!=='AbortError'&&ticket===state.ticket){$('status').textContent='';showError('读取失败：'+error.message+'。可重新点击标签重试。');}}
}
for(const b of document.querySelectorAll('[data-tag]'))b.onclick=()=>{state.tag=b.dataset.tag;state.page=1;loadPage();};
$('prev-page').onclick=()=>{if(state.page>1){state.page--;loadPage();}};
$('next-page').onclick=()=>{if(state.page<state.pages){state.page++;loadPage();}};
$('page-number').onchange=e=>{state.page=Math.min(state.pages,Math.max(1,Math.trunc(Number(e.target.value)||1)));loadPage();};
function repaint(){for(const card of [...state.cards,state.detail].filter(Boolean))paint(card);}
$('mode').onchange=e=>{state.mode=e.target.value;repaint();};
$('opacity').oninput=e=>{state.opacity=Number(e.target.value)/100;$('opacity-value').textContent=e.target.value+'%';repaint();};
$('close-detail').onclick=closeDetail;$('detail').addEventListener('cancel',e=>{e.preventDefault();closeDetail();});
window.addEventListener('resize',()=>{for(const card of [...state.cards,state.detail].filter(Boolean))fitCanvas(card);});
async function init(){
 try{const info=await json('/api/info');$('dataset').textContent=`${info.cluster} · ZYX ${info.shape.join(' × ')}`;
  for(const b of document.querySelectorAll('[data-tag]'))b.querySelector('span').textContent=info.counts[b.dataset.tag];
  await loadPage();
 }catch(error){$('status').textContent='';showError('初始化失败：'+error.message);}
}
init();
