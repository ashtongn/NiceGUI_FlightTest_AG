document.querySelectorAll('.spec').forEach((el,k)=>{const c=el.dataset.c||'#d0a644';let p='';const n=120;
for(let i=0;i<=n;i++){const x=i/n*600;const peak=Math.exp(-Math.pow((i-(40+k*25))/4,2))*60+Math.exp(-Math.pow((i-90)/2,2))*35;
const y=140-(28+Math.sin(i*1.7+k)*4+Math.random()*6+peak);p+=x.toFixed(1)+','+y.toFixed(1)+' ';}
let g='';for(let i=1;i<5;i++)g+=`<line x1="0" x2="600" y1="${i*30}" y2="${i*30}" stroke="#222822"/>`;
for(let i=1;i<6;i++)g+=`<line y1="0" y2="150" x1="${i*100}" x2="${i*100}" stroke="#222822"/>`;
el.innerHTML=`<svg viewBox="0 0 600 150" preserveAspectRatio="none">${g}<polyline fill="none" stroke="${c}" stroke-width="1.2" points="${p}"/></svg>`;});
