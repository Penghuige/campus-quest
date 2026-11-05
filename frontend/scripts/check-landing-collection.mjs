import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const cli=fileURLToPath(new URL('../node_modules/@playwright/test/cli.js',import.meta.url));
function collect(config){
 let output;
 try{output=execFileSync(process.execPath,[cli,'test','landing.integration.spec.ts','--list','--config',config,'--reporter=json'],{encoding:'utf8',maxBuffer:8*1024*1024});}
 catch(error){
  const report=JSON.parse(String(error.stdout??'{}'));
  if(error.status===1&&report.errors?.length===1&&report.errors[0].message?.includes('No tests found'))return [];
  throw error;
 }
 const report=JSON.parse(output);
 const files=[];
 function visit(node){if(node.file)files.push(node.file.replaceAll('\\','/'));for(const child of [...(node.suites??[]),...(node.specs??[])])visit(child);}
 visit(report);return files;
}
const shared=collect('playwright.config.ts');
assert.ok(!shared.some(file=>file.endsWith('landing.integration.spec.ts')),'shared stack must not collect the independently owned public Landing suite');
const dedicated=collect('playwright.landing.config.ts');
assert.ok(dedicated.length>0&&dedicated.every(file=>file.endsWith('landing.integration.spec.ts')),'dedicated gate must own exactly the Landing spec');
console.log('Landing collection isolation OK: shared battery excludes public suite; dedicated gate retains it.');
