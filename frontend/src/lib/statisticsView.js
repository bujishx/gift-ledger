const pad = n => String(n).padStart(2,'0');
export const statisticsTabs = [{id:'overview',label:'收支概况'},{id:'events',label:'事件分析'},{id:'relationships',label:'关系分析'}];
export function statisticsTab(value) { return statisticsTabs.some(tab => tab.id === value) ? value : 'overview'; }
export function dateText(date) { return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`; }
const parse = text => new Date(`${text}T12:00:00`);
const shiftDays = (value,days) => { const result = new Date(value); result.setDate(result.getDate()+days); return result; };
function shiftMonths(value,months) { const result = new Date(value); const day=result.getDate(); result.setDate(1); result.setMonth(result.getMonth()+months); result.setDate(Math.min(day,new Date(result.getFullYear(),result.getMonth()+1,0).getDate())); return result; }
export const periodOptions = [{id:'ytd',label:'本年累计'},{id:'month',label:'本月'},{id:'lastMonth',label:'上月'},{id:'quarter',label:'本季度'},{id:'lastQuarter',label:'上季度'},{id:'30days',label:'最近30天'},{id:'3months',label:'最近3个月'},{id:'12months',label:'最近12个月'},{id:'lastYear',label:'去年全年'},{id:'year',label:'指定年份'},{id:'selectedMonth',label:'指定月份'},{id:'all',label:'全部时间'},{id:'custom',label:'自定义日期'}];
export function periodRange(preset, {now=new Date(),year=now.getFullYear(),month=dateText(now).slice(0,7),from='',to=''}={}) {
  const end=new Date(now.getFullYear(),now.getMonth(),now.getDate(),12);
  const first=(y,m) => new Date(y,m,1,12), last=(y,m)=>new Date(y,m+1,0,12);
  const quarter=Math.floor(now.getMonth()/3)*3;
  const ranges={ytd:[first(now.getFullYear(),0),end],month:[first(now.getFullYear(),now.getMonth()),end],lastMonth:[first(now.getFullYear(),now.getMonth()-1),last(now.getFullYear(),now.getMonth()-1)],quarter:[first(now.getFullYear(),quarter),end],lastQuarter:[first(now.getFullYear(),quarter-3),last(now.getFullYear(),quarter-1)],'30days':[shiftDays(end,-29),end],'3months':[shiftDays(shiftMonths(end,-3),1),end],'12months':[shiftDays(shiftMonths(end,-12),1),end],lastYear:[first(now.getFullYear()-1,0),last(now.getFullYear()-1,11)],year:[first(Number(year),0),last(Number(year),11)],selectedMonth:[parse(month+'-01'),last(Number(month.slice(0,4)),Number(month.slice(5))-1)]};
  if(preset==='all') return {date_from:'',date_to:''};
  if(preset==='custom') { if(!/^\d{4}-\d{2}-\d{2}$/.test(from)||!/^\d{4}-\d{2}-\d{2}$/.test(to)||from>to) throw new Error('请填写有效的起止日期，开始日期不能晚于结束日期'); return {date_from:from,date_to:to}; }
  if(!ranges[preset] || ranges[preset].some(d=>Number.isNaN(d.valueOf()) || d.getFullYear()<1000 || d.getFullYear()>9999)) throw new Error('请选择有效的时间段');
  return {date_from:dateText(ranges[preset][0]),date_to:dateText(ranges[preset][1])};
}
export function ledgerStatisticsParams(params) {
  const {comparison_from,comparison_to,grain,limit,offset,sort_by,sort_order,...filters}=params;
  return Object.fromEntries(Object.entries(filters).filter(([,v])=>v!=='' && v!==null && v!==undefined));
}

/** 下钻条件与已生效条件取交集。额外单项类型/关系与原多选组均交给服务端AND。 */
export function statisticsDrillParams(applied = {}, extra = {}) {
  const params = { ...applied, ...extra };
  if (extra.date_from || extra.date_to) {
    params.date_from = [applied.date_from, extra.date_from].filter(Boolean).sort().at(-1) || '';
    params.date_to = [applied.date_to, extra.date_to].filter(Boolean).sort()[0] || '';
  }
  return params;
}
