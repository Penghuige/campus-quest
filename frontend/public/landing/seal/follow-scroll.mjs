export function followScroll(current,target,dt,tau=.18){
  if(![current,target,dt,tau].every(Number.isFinite)||dt<0||tau<=0)throw new RangeError('finite follow values, nonnegative dt and positive tau required');
  return current+(target-current)*(1-Math.exp(-dt/tau));
}
