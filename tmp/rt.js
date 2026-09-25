const fs=require('fs');
const snap=JSON.parse(fs.readFileSync('/tmp/snap.json','utf8')).state;
const h=fs.readFileSync('/tmp/page.html','utf8');
const js=h.split('<script>')[1].split('</scr'+'ipt>')[0];
const out={};
const mk=id=>({set innerHTML(v){out[id]=String(v)},get innerHTML(){return ''},
 set textContent(v){out[id]=String(v)},get textContent(){return ''},
 set className(v){},style:{},parentElement:{style:{}},addEventListener(){},
 querySelectorAll:()=>[],appendChild(){},remove(){},querySelector:()=>null,
 scrollTop:0,scrollHeight:0,value:'',setAttribute(){},getAttribute:()=>null});
global.window={addEventListener(){},__build:null};
global.document={getElementById:mk,querySelectorAll:()=>[],createElement:()=>mk('x'),addEventListener(){}};
global.EventSource=function(){return{addEventListener(){},close(){}}};
global.fetch=()=>Promise.resolve({json:()=>Promise.resolve({build:'1'})});
global.setInterval=()=>0; global.location={reload(){}};
try{
  eval(js);
  Object.assign(state,snap);
  render();
  const clean=s=>String(s||'(EMPTY)').replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();
  console.log('  headline:', clean(out.headline).slice(0,160));
  console.log('  causes  :', clean(out.causes).slice(0,70));
}catch(e){ console.log('  RENDER THREW:', e.message); }
