import {el,clear,fmtTime,mediaUrl,loadErrorCard} from '../util.js';
import {Api} from '../api.js';

export default {title:'反向寻车',async render(root){
 let active=true,epoch=0;
 const input=el('input',{id:'find-plate',placeholder:'例如 沪UD76840',maxlength:16,autocomplete:'off',required:true});
 const button=el('button',{type:'submit',class:'btn primary',text:'查找车辆'}),output=el('section',{'aria-live':'polite','aria-atomic':'true',class:'find-result'});
 const form=el('form',{class:'card filters'},[el('div',{class:'field'},[el('label',{for:'find-plate',text:'完整车牌'}),input]),button]);
 root.append(el('div',{class:'view-head'},[el('h2',{text:'反向寻车'}),el('span',{class:'sub',text:'查询当前停车位置与入场照片'})]),form,output);
 output.append(el('div',{class:'empty',text:'输入完整车牌开始查询'}));
 form.addEventListener('submit',async e=>{e.preventDefault();const turn=++epoch;button.disabled=true;clear(output);output.setAttribute('aria-busy','true');output.append(el('div',{class:'empty',text:'正在查找车辆…'}));
 try{const result=await Api.findCar(input.value.trim());const layout=result.located?await Api.layout():null;if(!active||turn!==epoch)return;clear(output);
 const location=el('div',{class:'card'},[el('h3',{text:result.plate}),el('p',{class:'find-space',text:result.located?`车位 ${result.space_no}`:result.message}),el('p',{text:'入场时间：'+fmtTime(result.entry_time)})]);
 if(result.located){const steps=el('ol',{class:'find-steps'});result.navigation.forEach(step=>steps.append(el('li',{text:step})));location.append(steps);}
 const url=mediaUrl(result.entry_photo||result.entry_plate_crop);location.append(el('h3',{text:'入场照片'}),url?el('img',{class:'find-photo',src:url,alt:result.plate+' 入场照片',onerror:e=>e.target.replaceWith(el('p',{text:'入场照片暂不可用'}))}):el('p',{text:'该记录未留存入场照片'}));output.append(location);
 if(layout)output.append(el('div',{class:'card'},[el('h3',{text:'车位位置指引'}),locationMap(layout,result.space_no)]));
 }catch(error){if(active&&turn===epoch){clear(output);output.append(el('div',{class:'card'},[loadErrorCard(error.message||'查询失败，请重试',()=>form.requestSubmit(),{title:'查询失败'})]));}}
 finally{if(active&&turn===epoch){button.disabled=false;output.removeAttribute('aria-busy');}}});
 return()=>{active=false;epoch++;};
}};

function locationMap(layout,target){
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 800 560');svg.setAttribute('role','img');svg.setAttribute('aria-label',`停车场车位图，目标车位 ${target}，北方在上方`);svg.classList.add('find-map');
 const add=(type,attrs,text)=>{const node=document.createElementNS(ns,type);Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,v));if(text)node.textContent=text;svg.append(node);return node;};
 add('rect',{x:0,y:0,width:800,height:560,fill:'#f4f6fb',rx:10});
 const X=x=>400+x*9,Y=z=>295-z*7;
 for(const space of layout.spaces){const highlight=space.space_no===target;add('rect',{x:X(space.x)-12,y:Y(space.z)-20,width:24,height:40,rx:2,fill:highlight?'#a32515':'#d8e2ee',stroke:highlight?'#6f130b':'#7b8a9d','stroke-width':highlight?3:1,'data-space':space.space_no});if(highlight)add('text',{x:X(space.x),y:Y(space.z)-27,'text-anchor':'middle',fill:'#8b160b','font-size':18,'font-weight':'bold'},target);}
 const zones=new Map();for(const space of layout.spaces){if(!zones.has(space.zone))zones.set(space.zone,[]);zones.get(space.zone).push(space);}
 for(const [zone,spaces] of zones){const x=spaces.reduce((v,s)=>v+s.x,0)/spaces.length,z=spaces.reduce((v,s)=>v+s.z,0)/spaces.length;add('text',{x:X(x),y:Y(z)+5,'text-anchor':'middle',fill:'#25334c','font-size':20},zone+'区');}
 add('text',{x:400,y:30,'text-anchor':'middle',fill:'#25334c','font-size':18},'北 ↑');add('text',{x:X(-3.5),y:535,'text-anchor':'middle',fill:'#25334c','font-size':16},'入口');
 return svg;
}
