import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseTalentQuery, buildTalentQuery, talentListHref } from '../features/innovation/talentQuery';
test('keyword, all-match tags, cooperation state and page survive list-detail return', () => {
  const tags=['00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002'];
  const query={q:'模拟_% 项目',tag_ids:tags,cooperation_state:'NOT_AVAILABLE' as const,has_public_materials:'' as const,offset:40};
  const params=buildTalentQuery(query);
  assert.deepEqual(parseTalentQuery(params),query);
  assert.equal(talentListHref(params.toString()),'/innovation/talents?'+params.toString());
});
test('material facts preserve true and false through pagination and detail return', () => {
  for (const value of ['true','false'] as const) {
    const parsed=parseTalentQuery(new URLSearchParams(`has_public_materials=${value}&offset=40&q=模拟`));
    assert.equal(parsed.has_public_materials,value);
    assert.equal(buildTalentQuery(parsed).get('has_public_materials'),value);
    assert.match(talentListHref(buildTalentQuery(parsed).toString()),new RegExp(`has_public_materials=${value}`));
    assert.equal(buildTalentQuery(parsed,0).has('offset'),false);
  }
  for (const value of ['', 'verified', '1', 'undefined']) {
    const parsed=parseTalentQuery(new URLSearchParams(`has_public_materials=${value}`));
    assert.equal(parsed.has_public_materials,'');
    assert.equal(buildTalentQuery(parsed).has('has_public_materials'),false);
  }
});
test('query construction excludes unrelated private fields and deduplicates tags', () => {
  const tag='00000000-0000-4000-8000-000000000001';
  const query=parseTalentQuery(new URLSearchParams(`tag_ids=${tag}&tag_ids=${tag}&offset=-1&phone=private&from=https://example.invalid`));
  assert.deepEqual(query.tag_ids,[tag]); assert.equal(query.offset,0);
  assert.ok(!buildTalentQuery(query).toString().includes('private'));
  assert.ok(talentListHref('from=https://example.invalid').startsWith('/innovation/talents'));
});
