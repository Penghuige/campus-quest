import test from 'node:test';
import assert from 'node:assert/strict';
import { finalPresentation } from '../../public/landing/seal/presentation.mjs';

test('STATIC always exposes the clickable DOM imprint after a Canvas comparison', () => {
  assert.deepEqual(finalPresentation('STATIC', false), { useDOM: true, allowComparison: false,showCanvas:true,animatePaper:false });
});

test('DONE allows inspection of Canvas and returns to the clickable DOM imprint', () => {
  assert.deepEqual(finalPresentation('DONE', false), { useDOM: false, allowComparison: true,showCanvas:true,animatePaper:true });
  assert.deepEqual(finalPresentation('DONE', true), { useDOM: true, allowComparison: true,showCanvas:true,animatePaper:true });
});
