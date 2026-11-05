export function getLoginURL(moduleURL){
 const current=new URL(moduleURL);
 if(!['http:','https:'].includes(current.protocol))throw new TypeError('A web origin is required');
 return new URL('../login',current).href;
}
export function navigateToLogin(){window.open(getLoginURL(import.meta.url),'_top');}
