import test from 'node:test';
import assert from 'node:assert/strict';

const sceneModule = await import('../../public/landing/seal/scene-switch.mjs').catch(() => ({}));

function fixture(options = {}) {
  assert.equal(typeof sceneModule.SceneSwitch, 'function', 'SceneSwitch must be exported');
  const changes = [];
  const controller = new sceneModule.SceneSwitch({
    ...options,
    onChange: snapshot => changes.push(snapshot),
  });
  return { controller, changes };
}

test('each valid initial scene starts idle without notifying a change', () => {
  for (const scene of ['hero', 'intro', 'seal']) {
    const { controller, changes } = fixture({ initial: scene });
    assert.deepEqual(controller.snapshot(), {
      scene, from: null, to: null, phase: 'idle', token: null,
    });
    assert.equal(changes.length, 0);
  }
  assert.equal(fixture().controller.snapshot().scene, 'hero');
});

test('a scene cannot change before an explicit request', () => {
  const { controller, changes } = fixture();
  const initial = controller.snapshot();
  for (const token of [undefined, null, Object.freeze({})]) {
    assert.equal(controller.midpoint(token), false);
    assert.equal(controller.complete(token), false);
  }
  assert.deepEqual(controller.snapshot(), initial);
  assert.equal(changes.length, 0);
});

test('a request keeps its target hidden until its own midpoint', () => {
  const { controller, changes } = fixture();
  const initial = controller.snapshot();
  const token = controller.request('intro');
  assert.ok(token);
  assert.deepEqual(controller.snapshot(), {
    scene: 'hero', from: 'hero', to: 'intro', phase: 'out', token,
  });
  assert.equal(controller.complete(token), false);
  assert.equal(controller.midpoint(Object.freeze({})), false);
  assert.equal(controller.snapshot().scene, 'hero');
  assert.equal(initial.scene, 'hero');
  assert.equal(initial.phase, 'idle');
  assert.equal(changes.length, 1);
  assert.equal(controller.midpoint(token), true);
  assert.deepEqual(controller.snapshot(), {
    scene: 'intro', from: 'hero', to: 'intro', phase: 'in', token,
  });
  assert.equal(controller.midpoint(token), false);
  assert.equal(changes.length, 2);
});

test('completion clears the transition only after the committed midpoint', () => {
  const { controller, changes } = fixture();
  const token = controller.request('intro');
  controller.midpoint(token);
  assert.equal(controller.complete(token), true);
  assert.deepEqual(controller.snapshot(), {
    scene: 'intro', from: null, to: null, phase: 'idle', token: null,
  });
  assert.equal(controller.complete(token), false);
  assert.equal(controller.midpoint(token), false);
  assert.equal(changes.length, 3);
});

test('rapid repeated clicks are rejected during both transition phases', () => {
  const { controller, changes } = fixture();
  const token = controller.request('intro');
  const outgoing = controller.snapshot();
  assert.equal(controller.request('seal'), null);
  assert.equal(controller.request('intro'), null);
  assert.equal(controller.request('hero'), null);
  assert.deepEqual(controller.snapshot(), outgoing);
  assert.equal(changes.length, 1);
  controller.midpoint(token);
  const incoming = controller.snapshot();
  assert.equal(controller.request('hero'), null);
  assert.equal(controller.request('seal'), null);
  assert.deepEqual(controller.snapshot(), incoming);
  assert.equal(changes.length, 2);
});

test('an idle request for the current scene is a silent no-op', () => {
  const { controller, changes } = fixture({ initial: 'intro' });
  const initial = controller.snapshot();
  assert.equal(controller.request('intro'), null);
  assert.equal(controller.request('intro', { interrupt: true }), null);
  assert.deepEqual(controller.snapshot(), initial);
  assert.equal(changes.length, 0);
});

test('stale callbacks cannot advance a later transition', () => {
  const { controller, changes } = fixture();
  const first = controller.request('intro');
  controller.midpoint(first);
  controller.complete(first);
  const second = controller.request('seal');
  assert.notEqual(second, first);
  assert.equal(controller.midpoint(first), false);
  controller.midpoint(second);
  const incoming = controller.snapshot();
  const notifications = changes.length;
  assert.equal(controller.complete(first), false);
  assert.deepEqual(controller.snapshot(), incoming);
  assert.equal(changes.length, notifications);
  assert.equal(controller.complete(second), true);
  assert.equal(controller.snapshot().scene, 'seal');
});

test('both later scenes can explicitly transition back to hero', () => {
  for (const initial of ['intro', 'seal']) {
    const { controller } = fixture({ initial });
    const token = controller.request('hero');
    assert.equal(controller.snapshot().scene, initial);
    assert.equal(controller.snapshot().from, initial);
    assert.equal(controller.midpoint(token), true);
    assert.equal(controller.snapshot().scene, 'hero');
    assert.equal(controller.complete(token), true);
    assert.equal(controller.snapshot().phase, 'idle');
  }
});

test('cancel before midpoint keeps the committed scene and invalidates its token', () => {
  const { controller, changes } = fixture();
  const token = controller.request('intro');
  controller.cancel();
  assert.deepEqual(controller.snapshot(), {
    scene: 'hero', from: null, to: null, phase: 'idle', token: null,
  });
  assert.equal(controller.midpoint(token), false);
  assert.equal(controller.complete(token), false);
  assert.equal(changes.length, 2);
  controller.cancel();
  assert.equal(changes.length, 2);
  assert.notEqual(controller.request('intro'), token);
});

test('cancel after midpoint keeps the newly committed scene', () => {
  const { controller } = fixture();
  const token = controller.request('intro');
  controller.midpoint(token);
  controller.cancel();
  assert.deepEqual(controller.snapshot(), {
    scene: 'intro', from: null, to: null, phase: 'idle', token: null,
  });
  assert.equal(controller.complete(token), false);
});

test('an interrupted popstate before midpoint follows the new destination', () => {
  const { controller } = fixture();
  const first = controller.request('intro');
  const popstate = controller.request('seal', { interrupt: true });
  assert.notEqual(popstate, first);
  assert.deepEqual(controller.snapshot(), {
    scene: 'hero', from: 'hero', to: 'seal', phase: 'out', token: popstate,
  });
  assert.equal(controller.midpoint(first), false);
  assert.equal(controller.complete(first), false);
  assert.equal(controller.midpoint(popstate), true);
  assert.equal(controller.complete(popstate), true);
  assert.equal(controller.snapshot().scene, 'seal');
});

test('an interrupted popstate after midpoint starts from the committed scene', () => {
  const { controller } = fixture();
  const first = controller.request('intro');
  controller.midpoint(first);
  const popstate = controller.request('hero', { interrupt: true });
  assert.deepEqual(controller.snapshot(), {
    scene: 'intro', from: 'intro', to: 'hero', phase: 'out', token: popstate,
  });
  assert.equal(controller.complete(first), false);
  assert.equal(controller.midpoint(popstate), true);
  assert.equal(controller.complete(popstate), true);
  assert.equal(controller.snapshot().scene, 'hero');
});

test('browser Back before midpoint cancels a transition to the current scene', () => {
  const { controller, changes } = fixture();
  const first = controller.request('intro');
  assert.equal(controller.request('hero', { interrupt: true }), null);
  assert.deepEqual(controller.snapshot(), {
    scene: 'hero', from: null, to: null, phase: 'idle', token: null,
  });
  assert.equal(controller.midpoint(first), false);
  assert.equal(controller.complete(first), false);
  assert.equal(changes.length, 2);
});

test('snapshots and opaque tokens cannot be mutated by consumers', () => {
  const { controller, changes } = fixture();
  const initial = controller.snapshot();
  assert.equal(Object.isFrozen(initial), true);
  assert.throws(() => { initial.scene = 'seal'; }, TypeError);
  const token = controller.request('intro');
  const outgoing = controller.snapshot();
  assert.equal(Object.isFrozen(token), true);
  assert.equal(Object.isFrozen(outgoing), true);
  assert.equal(Object.isFrozen(changes.at(-1)), true);
  assert.throws(() => { token.scene = 'seal'; }, TypeError);
  assert.throws(() => { outgoing.to = 'seal'; }, TypeError);
  assert.equal(controller.midpoint({ ...token }), false);
  assert.equal(controller.midpoint(token), true);
  assert.equal(outgoing.phase, 'out');
  assert.equal(initial.phase, 'idle');
  assert.equal(controller.snapshot().scene, 'intro');
});

test('invalid scene IDs are rejected without changing state or notifying', () => {
  assert.equal(typeof sceneModule.SceneSwitch, 'function', 'SceneSwitch must be exported');
  const invalid = ['', 'unknown', 'Hero', null, 0, {}, new String('hero')];
  for (const initial of invalid) {
    assert.throws(() => new sceneModule.SceneSwitch({ initial }), TypeError);
  }
  assert.throws(() => new sceneModule.SceneSwitch({ onChange: null }), TypeError);
  const { controller, changes } = fixture();
  for (const to of [...invalid, undefined]) {
    assert.throws(() => controller.request(to), TypeError);
  }
  const token = controller.request('intro');
  const outgoing = controller.snapshot();
  for (const to of [...invalid, undefined]) {
    assert.throws(() => controller.request(to, { interrupt: true }), TypeError);
  }
  assert.deepEqual(controller.snapshot(), outgoing);
  assert.equal(controller.snapshot().token, token);
  assert.equal(changes.length, 1);
});
