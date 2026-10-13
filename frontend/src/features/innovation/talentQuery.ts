import type { TalentState } from './talentApi';

export type TalentQueryState={q:string;tag_ids:string[];cooperation_state:TalentState|'';offset:number};
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function parseTalentQuery(params:URLSearchParams):TalentQueryState {
  const state=params.get('cooperation_state'); const raw=params.get('offset')??'0'; const offset=Number(raw);
  return {q:params.get('q')??'',tag_ids:[...new Set(params.getAll('tag_ids').filter(id=>uuid.test(id)))].slice(0,5),cooperation_state:state==='LOOKING'||state==='OPEN_TO_OPPORTUNITIES'||state==='NOT_AVAILABLE'?state:'',offset:/^\d+$/.test(raw)&&Number.isSafeInteger(offset)?offset:0};
}
export function buildTalentQuery(query:TalentQueryState,offset=query.offset):URLSearchParams {
  const params=new URLSearchParams();
  if (query.q.trim()) params.set('q',query.q.trim());
  for (const id of query.tag_ids) params.append('tag_ids',id);
  if (query.cooperation_state) params.set('cooperation_state',query.cooperation_state);
  if (offset>0) params.set('offset',String(offset));
  return params;
}
/** Returns a local, whitelisted destination even when the incoming URL is arbitrary. */
export function talentListHref(query:string):string {
  const params=buildTalentQuery(parseTalentQuery(new URLSearchParams(query)));
  return `/innovation/talents${params.size?'?'+params:''}`;
}
