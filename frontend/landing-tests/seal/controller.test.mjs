import test from 'node:test';
import assert from 'node:assert/strict';
import { SealController } from '../../public/landing/seal/controller.mjs';

function setup(options = {}) {
  const changes = [];
  const starts = [];
  const cancels = [];
  const dones = [];
  const controller = new SealController({
    start: 100,
    contact: 200,
    onChange: snapshot => changes.push(snapshot),
    onStartAuto: (token, snapshot) => starts.push({ token, snapshot }),
    onCancel: (token, snapshot) => cancels.push({ token, snapshot }),
    onDone: snapshot => dones.push(snapshot),
    ...options,
  });
  return { controller, changes, starts, cancels, dones };
}

test('the start boundary belongs to APPROACH and descent follows local scroll', () => {
  const { controller, starts } = setup();
  assert.deepEqual(controller.updateScroll(99), { state: 'BEFORE', approach: 0, token: 0 });
  assert.deepEqual(controller.updateScroll(100), { state: 'APPROACH', approach: 0, token: 0 });
  assert.deepEqual(controller.updateScroll(150), { state: 'APPROACH', approach: 0.5, token: 0 });
  assert.deepEqual(controller.updateScroll(199), { state: 'APPROACH', approach: 0.99, token: 0 });
  assert.equal(starts.length, 0);
});

test('reversing before contact rewinds descent without starting automatic playback', () => {
  const { controller, starts } = setup();
  controller.updateScroll(180);
  assert.equal(controller.updateScroll(120).approach, 0.2);
  assert.equal(controller.updateScroll(99).state, 'BEFORE');
  assert.equal(starts.length, 0);
});

test('contact and an initial jump beyond contact each start one playback', () => {
  for (const y of [200, 10000]) {
    const { controller, starts } = setup();
    const snapshot = controller.updateScroll(y);
    assert.equal(snapshot.state, 'AUTO');
    assert.equal(snapshot.approach, 1);
    assert.ok(snapshot.token > 0);
    assert.deepEqual(starts, [{ token: snapshot.token, snapshot }]);
    controller.updateScroll(y + 1);
    controller.updateScroll(150);
    assert.equal(starts.length, 1);
  }
});

test('automatic playback continues during reversal inside the inclusive start boundary', () => {
  const { controller, cancels } = setup();
  const initial = controller.updateScroll(200);
  assert.deepEqual(controller.updateScroll(150), initial);
  assert.deepEqual(controller.updateScroll(100), initial);
  assert.equal(cancels.length, 0);
  assert.equal(controller.complete(initial.token), true);
  assert.equal(controller.snapshot().state, 'DONE');
});

test('leaving above start cancels immediately and rejects the old completion', () => {
  const { controller, cancels, dones } = setup();
  const active = controller.updateScroll(200);
  const reset = controller.updateScroll(99);
  assert.equal(reset.state, 'BEFORE');
  assert.equal(reset.approach, 0);
  assert.ok(reset.token > active.token);
  assert.deepEqual(cancels, [{ token: active.token, snapshot: reset }]);
  assert.equal(controller.complete(active.token), false);
  assert.deepEqual(controller.snapshot(), reset);
  assert.equal(dones.length, 0);
  controller.updateScroll(98);
  assert.equal(cancels.length, 1);
});

test('DONE remains terminal inside the region and replays only after leaving', () => {
  const { controller, starts, dones } = setup();
  const first = controller.updateScroll(200);
  assert.equal(controller.complete(first.token), true);
  assert.equal(controller.complete(first.token), false);
  assert.equal(dones.length, 1);
  for (const y of [100, 150, 200, 10000]) {
    assert.equal(controller.updateScroll(y).state, 'DONE');
  }
  assert.equal(starts.length, 1);
  controller.updateScroll(99);
  const replay = controller.updateScroll(200);
  assert.equal(replay.state, 'AUTO');
  assert.ok(replay.token > first.token);
  assert.equal(starts.length, 2);
});

test('an old completion cannot finish a newer playback', () => {
  const { controller } = setup();
  const first = controller.updateScroll(200);
  controller.updateScroll(99);
  const second = controller.updateScroll(200);
  assert.equal(controller.complete(first.token), false);
  assert.deepEqual(controller.snapshot(), second);
  assert.equal(controller.complete(second.token), true);
});

test('thirty consecutive cancel and replay cycles retain one current generation', () => {
  const { controller, starts, cancels, dones } = setup();
  const tokens = [];
  for (let cycle = 0; cycle < 30; cycle += 1) {
    const current = controller.updateScroll(200);
    assert.equal(current.state, 'AUTO');
    for (const oldToken of tokens) assert.equal(controller.complete(oldToken), false);
    tokens.push(current.token);
    assert.equal(controller.updateScroll(100).state, 'AUTO');
    assert.equal(controller.updateScroll(99).state, 'BEFORE');
    assert.equal(controller.complete(current.token), false);
  }
  assert.equal(new Set(tokens).size, 30);
  assert.equal(starts.length, 30);
  assert.equal(cancels.length, 30);
  assert.equal(dones.length, 0);
  const final = controller.updateScroll(200);
  assert.equal(controller.complete(final.token), true);
  assert.equal(controller.snapshot().state, 'DONE');
  assert.equal(dones.length, 1);
});

test('resize keeps an active playback and its completion token while still in the region', () => {
  const { controller, starts, cancels } = setup();
  const active = controller.updateScroll(200);
  assert.deepEqual(controller.reconfigure({ start: 90, contact: 300 }, 120), active);
  assert.equal(starts.length, 1);
  assert.equal(cancels.length, 0);
  assert.equal(controller.complete(active.token), true);
});

test('resize cancels an active playback if the new start is below the scroll position', () => {
  const { controller, cancels } = setup();
  const active = controller.updateScroll(200);
  assert.equal(controller.reconfigure({ start: 250, contact: 350 }, 200).state, 'BEFORE');
  assert.equal(cancels.length, 1);
  assert.equal(controller.complete(active.token), false);
});

test('resize recalculates descent but does not itself trigger a new contact', () => {
  const { controller, starts } = setup();
  controller.updateScroll(150);
  assert.equal(controller.reconfigure({ start: 50, contact: 250 }, 150).approach, 0.5);
  assert.deepEqual(
    controller.reconfigure({ start: 50, contact: 140 }, 150),
    { state: 'APPROACH', approach: 1, token: 0 },
  );
  assert.equal(starts.length, 0);
  assert.equal(controller.updateScroll(151).state, 'AUTO');
  assert.equal(starts.length, 1);
});

test('DONE survives resize inside the region and resets when resize moves it outside', () => {
  const { controller, starts } = setup();
  const active = controller.updateScroll(200);
  controller.complete(active.token);
  assert.equal(controller.reconfigure({ start: 50, contact: 300 }, 120).state, 'DONE');
  assert.equal(starts.length, 1);
  assert.equal(controller.reconfigure({ start: 150, contact: 300 }, 120).state, 'BEFORE');
});

test('reduced motion cancels playback and keeps a static usable terminal state', () => {
  const { controller, cancels, starts } = setup();
  const active = controller.updateScroll(200);
  const reduced = controller.setReducedMotion(true, 200);
  assert.equal(reduced.state, 'STATIC');
  assert.equal(reduced.approach, 1);
  assert.ok(reduced.token > active.token);
  assert.equal(cancels.length, 1);
  assert.equal(controller.complete(active.token), false);
  assert.deepEqual(controller.updateScroll(0), reduced);
  assert.deepEqual(controller.reconfigure({ start: 50, contact: 150 }, 0), reduced);
  assert.equal(starts.length, 1);
});

test('leaving reduced motion recalibrates from the actual current scroll position', () => {
  for (const [y, expectedState] of [[0, 'BEFORE'], [150, 'APPROACH'], [200, 'AUTO']]) {
    const { controller } = setup();
    controller.setReducedMotion(true, y);
    assert.equal(controller.setReducedMotion(false, y).state, expectedState);
  }
});

test('duplicate static updates do not emit changes or cancellation', () => {
  const { controller, changes, cancels } = setup();
  const staticState = controller.setReducedMotion(true, 0);
  assert.deepEqual(controller.setReducedMotion(true, 50), staticState);
  assert.equal(changes.length, 1);
  assert.equal(cancels.length, 0);
});

test('completion outside AUTO is rejected and snapshots cannot mutate internal state', () => {
  const { controller } = setup();
  assert.equal(controller.complete(0), false);
  controller.updateScroll(150);
  const snapshot = controller.snapshot();
  snapshot.state = 'DONE';
  snapshot.approach = 1;
  assert.deepEqual(controller.snapshot(), { state: 'APPROACH', approach: 0.5, token: 0 });
});

test('changed snapshots precede automatic start and completion callbacks', () => {
  const events = [];
  const controller = new SealController({
    start: 100,
    contact: 200,
    onChange: snapshot => events.push(`change:${snapshot.state}`),
    onStartAuto: () => events.push('start'),
    onDone: () => events.push('done'),
  });
  const active = controller.updateScroll(200);
  controller.complete(active.token);
  assert.deepEqual(events, ['change:AUTO', 'start', 'change:DONE', 'done']);
});

test('invalid geometry and non-finite scroll positions cannot corrupt controller state', () => {
  assert.throws(() => setup({ start: 200, contact: 200 }), RangeError);
  assert.throws(() => setup({ start: 300, contact: 200 }), RangeError);
  assert.throws(() => setup({ start: NaN }), TypeError);
  const { controller } = setup();
  controller.updateScroll(150);
  const before = controller.snapshot();
  assert.throws(() => controller.updateScroll(Infinity), TypeError);
  assert.throws(() => controller.reconfigure({ start: 150, contact: 150 }, 150), RangeError);
  assert.deepEqual(controller.snapshot(), before);
});
