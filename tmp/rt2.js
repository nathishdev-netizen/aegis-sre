const fs=require('fs');
const snap=JSON.parse(fs.readFileSync('/tmp/snap.json','utf8')).state;
const h=fs.readFileSync('/tmp/page.html','utf8');
const js=h.split('<script>')[1].split('</scr'+'ipt>')[0];
const out={};
const mk=id=>({set innerHTML(v){out[id]=String(v)},get innerHTML(){return ''},
 set textContent(v){out[id]=String(v)},get textContent(){return ''},
 set className(v){},set title(v){},style:{},parentElement:{style:{}},addEventListener(){},
 querySelectorAll:()=>[],appendChild(){},remove(){},querySelector:()=>null,
 scrollTop:0,scrollHeight:0,value:'',setAttribute(){},getAttribute:()=>null});
global.window={addEventListener(){}};
global.document={getElementById:mk,querySelectorAll:()=>[],createElement:()=>mk('x'),addEventListener(){}};
global.EventSource=function(){return{addEventListener(){},close(){}}};
global.fetch=()=>Promise.resolve({json:()=>Promise.resolve({build:'1'})});
global.setInterval=()=>0; global.location={reload(){}};
// Run the page script and its render together, inside one scope.
const runner = new Function('snap','out', js + '\n;Object.assign(state,snap);render();');
try{
  runner(snap,out);
  const clean=s=>String(s||'(EMPTY)').replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();
  console.log('  headline :', clean(out.headline).slice(0,170));
  console.log('  causes   :', clean(out.causes).slice(0,70));
  console.log('  statsPill:', clean(out.statsPills).slice(0,70));
}catch(e){ console.log('  RENDER THREW:', e.message); }
