const scenes = new Set(['hero', 'intro', 'seal']);

function validateScene(scene) {
  if (!scenes.has(scene)) throw new TypeError('scene must be hero, intro, or seal');
}

function idleState(scene) {
  return { scene, from: null, to: null, phase: 'idle', token: null };
}

/** Pure scene state. The caller owns input, animation, and the midpoint signal. */
export class SceneSwitch {
  #state;
  #onChange;

  constructor({ initial = 'hero', onChange = () => {} } = {}) {
    validateScene(initial);
    if (typeof onChange !== 'function') throw new TypeError('onChange must be a function');
    this.#state = Object.freeze(idleState(initial));
    this.#onChange = onChange;
  }

  snapshot() {
    return this.#state;
  }

  request(to, { interrupt = false } = {}) {
    validateScene(to);
    if (this.#state.phase !== 'idle' && !interrupt) return null;
    if (to === this.#state.scene) {
      this.cancel();
      return null;
    }

    // Identity makes tokens opaque: copied or stale tokens cannot own this turn.
    const token = Object.freeze({});
    this.#change({
      scene: this.#state.scene,
      from: this.#state.scene,
      to,
      phase: 'out',
      token,
    });
    return token;
  }

  midpoint(token) {
    if (this.#state.phase !== 'out' || token !== this.#state.token) return false;
    this.#change({ ...this.#state, scene: this.#state.to, phase: 'in' });
    return true;
  }

  complete(token) {
    if (this.#state.phase !== 'in' || token !== this.#state.token) return false;
    this.#change(idleState(this.#state.scene));
    return true;
  }

  cancel() {
    if (this.#state.phase === 'idle') return this.#state;
    return this.#change(idleState(this.#state.scene));
  }

  #change(state) {
    this.#state = Object.freeze(state);
    this.#onChange(this.#state);
    return this.#state;
  }
}
