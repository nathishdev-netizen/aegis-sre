const fs=require('fs');
const snap=JSON.parse(fs.readFileSync('/tmp/snap.json','utf8')).state;
const h=fs.readFileSync('/Users/nathish/Desktop/Nathish/explore/log/app/web/index.html','utf8');
const js=h.split('<script>')[1].split('</scr'+'ipt>')[0];
const written={};
const el=id=>({ set innerHTML(v){written[id]=String(v)}, get innerHTML(){return ''},
  set textContent(v){written[id]=String(v)}, get textContent(){return ''},
  set className(v){}, style:{}, parentElement:{style:{}},
  addEventListener(){}, querySelectorAll:()=>[], appendChild(){}, remove(){},
  querySelector:()=>null, scrollTop:0, scrollHeight:0, value:'', setAttribute(){}, getAttribute:()=>null });
global.window={agent:null,addEventListener(){}};
global.document={getElementById:id=>el(id),querySelectorAll:()=>[],createElement:()=>el('x'),addEventListener(){}};
global.EventSource=function(){return {addEventListener(){},close(){}}};
global.fetch=()=>Promise.resolve({json:()=>Promise.resolve({build:'1'})});
global.setInterval=()=>0; global.location={reload(){}};
try{
  eval(js);
  Object.assign(state, snap);
  render();
  const strip=s=>String(s||'(empty)').replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();
  console.log('headline  :', strip(written.headline).slice(0,140));
  console.log('summary   :', strip(written.summary).slice(0,90));
  console.log('causes    :', strip(written.causes).slice(0,90));
  console.log('fixes     :', strip(written.fixes).slice(0,90));
  console.log('statsPills:', strip(written.statsPills).slice(0,110));
}catch(e){ console.log('RENDER ERROR:', e.message); }
