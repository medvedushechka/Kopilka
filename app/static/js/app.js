function bindCalc(amountId, daysId, amountOutId, daysOutId, totalOutId){
  const amount = document.getElementById(amountId);
  const days = document.getElementById(daysId);
  const amountOut = document.getElementById(amountOutId);
  const daysOut = document.getElementById(daysOutId);
  const totalOut = document.getElementById(totalOutId);
  function recalc(){
    if(!amount || !days || !amountOut || !daysOut || !totalOut) return;
    const a = Number(amount.value); const d = Number(days.value);
    amountOut.textContent = a; daysOut.textContent = d;
    totalOut.textContent = (a + a * 0.008 * d).toFixed(2);
  }
  if(amount && days){ amount.addEventListener('input', recalc); days.addEventListener('input', recalc); recalc(); }
}
function initMobileNav(){
  const toggle = document.querySelector('.nav-toggle'); const nav = document.querySelector('.nav');
  if(!toggle || !nav) return; toggle.addEventListener('click', () => nav.classList.toggle('is-open'));
}
function initCounters(){
  const items = document.querySelectorAll('[data-counter]');
  if(!items.length) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if(!entry.isIntersecting) return;
      const el = entry.target; const target = Number(el.dataset.counter || 0); const duration = 900; const start = performance.now();
      function frame(now){
        const p = Math.min(1, (now - start) / duration); const eased = 1 - Math.pow(1-p, 3);
        el.textContent = Math.round(target * eased).toLocaleString('ru-RU');
        if(p < 1) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame); io.unobserve(el);
    });
  }, {threshold:.35});
  items.forEach(x => io.observe(x));
}
function initTransactionPulse(){
  document.querySelectorAll('.tx-row[data-status="В обработке"] em').forEach((el) => {
    const original = el.textContent.trim(); let step = 0;
    setInterval(() => { step = (step + 1) % 4; el.textContent = original + '.'.repeat(step); }, 650);
  });
}
function initHeroChart(){
  const canvas = document.getElementById('heroChart');
  if(!canvas || typeof Chart === 'undefined') return;
  new Chart(canvas, {
    type: 'line',
    data: { labels:['Янв','Фев','Мар','Апр','Май','Июн'], datasets:[{ data:[420,680,740,1120,1380,1690], tension:.42, fill:true, borderWidth:3, pointRadius:0 }] },
    options: { responsive:true, plugins:{legend:{display:false}, tooltip:{enabled:false}}, scales:{x:{display:false},y:{display:false}}, elements:{line:{borderColor:'#caa24a',backgroundColor:'rgba(202,162,74,.16)'}} }
  });
}
bindCalc('amount', 'days', 'amountOut', 'daysOut', 'totalOut');
bindCalc('applyAmount', 'applyDays', 'applyAmountOut', 'applyDaysOut', 'applyTotalOut');
initMobileNav(); initCounters(); initTransactionPulse(); initHeroChart();

function chartCommon(){
  return {
    responsive:true,
    maintainAspectRatio:false,
    plugins:{legend:{display:false}, tooltip:{backgroundColor:'#111827',padding:12,cornerRadius:12,titleFont:{weight:'700'},bodyFont:{weight:'600'}}},
    scales:{x:{grid:{display:false},ticks:{color:'#6b7280',font:{weight:'700'}}},y:{grid:{color:'rgba(17,24,39,.07)'},ticks:{color:'#6b7280',font:{weight:'700'}}}}
  };
}
function initProfitChart(){
  const canvas = document.getElementById('profitChart');
  if(!canvas || typeof Chart === 'undefined') return;
  const labels = JSON.parse(canvas.dataset.labels || '[]');
  const values = JSON.parse(canvas.dataset.values || '[]');
  new Chart(canvas,{type:'line',data:{labels,datasets:[{data:values,tension:.42,fill:true,borderWidth:3,pointRadius:4,pointHoverRadius:6}]},options:{...chartCommon(),elements:{line:{borderColor:'#111827',backgroundColor:'rgba(198,163,79,.16)'},point:{backgroundColor:'#c6a34f',borderColor:'#fff',borderWidth:3}}}});
}
function initRiskChart(){
  const canvas = document.getElementById('riskChart');
  if(!canvas || typeof Chart === 'undefined') return;
  const labels = JSON.parse(canvas.dataset.labels || '[]');
  const values = JSON.parse(canvas.dataset.values || '[]');
  new Chart(canvas,{type:'doughnut',data:{labels,datasets:[{data:values,borderWidth:0,hoverOffset:8}]},options:{responsive:true,maintainAspectRatio:false,cutout:'68%',plugins:{legend:{position:'bottom',labels:{usePointStyle:true,boxWidth:8,color:'#6b7280',font:{weight:'800'}}},tooltip:{backgroundColor:'#111827',padding:12,cornerRadius:12}}}});
}
initProfitChart();
initRiskChart();
function initPlatformGradeChart(){
  const canvas = document.getElementById('platformGradeChart');
  if(!canvas || typeof Chart === 'undefined') return;
  const labels = JSON.parse(canvas.dataset.labels || '[]');
  const values = JSON.parse(canvas.dataset.values || '[]');
  new Chart(canvas,{type:'bar',data:{labels,datasets:[{data:values,borderRadius:14,borderSkipped:false}]},options:{...chartCommon(),plugins:{...chartCommon().plugins,legend:{display:false}}}});
}
initPlatformGradeChart();

function initAdminDropdown(){
  const dropdown = document.querySelector('.nav-dropdown');
  if(!dropdown) return;
  const toggle = dropdown.querySelector('.nav-dropdown-toggle');
  if(!toggle) return;
  let closeTimer = null;
  const open = () => { clearTimeout(closeTimer); dropdown.classList.add('open'); };
  const close = () => { closeTimer = setTimeout(() => dropdown.classList.remove('open'), 180); };
  dropdown.addEventListener('mouseenter', open);
  dropdown.addEventListener('mouseleave', close);
  toggle.addEventListener('click', (event) => {
    if (window.matchMedia('(min-width: 981px)').matches) {
      event.preventDefault();
      dropdown.classList.toggle('open');
    }
  });
  document.addEventListener('click', (event) => {
    if(!dropdown.contains(event.target)) dropdown.classList.remove('open');
  });
}
initAdminDropdown();
