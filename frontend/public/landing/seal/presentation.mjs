export function finalPresentation(state, requestedDOM) {
  return { useDOM: state === 'STATIC' || requestedDOM, allowComparison: state === 'DONE',showCanvas:true,animatePaper:state==='DONE' };
}
