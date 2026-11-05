function finite(value, name) {
  if (!Number.isFinite(value)) throw new TypeError(`${name} must be finite`);
}

function validateBounds(start, contact) {
  finite(start, 'start');
  finite(contact, 'contact');
  if (contact <= start) throw new RangeError('contact must be greater than start');
}

/** Pure state control. The caller owns the timeline, rendering and its cleanup. */
export class SealController {
  #start;
  #contact;
  #lastY;
  #state = 'BEFORE';
  #approach = 0;
  #token = 0;
  #reduced = false;
  #onChange;
  #onStartAuto;
  #onCancel;
  #onDone;

  constructor({ start, contact, onChange = () => {}, onStartAuto = () => {},
    onCancel = () => {}, onDone = () => {} }) {
    validateBounds(start, contact);
    this.#start = start;
    this.#contact = contact;
    this.#lastY = start - 1;
    this.#onChange = onChange;
    this.#onStartAuto = onStartAuto;
    this.#onCancel = onCancel;
    this.#onDone = onDone;
  }

  snapshot() {
    return { state: this.#state, approach: this.#approach, token: this.#token };
  }

  updateScroll(y) {
    finite(y, 'scroll position');
    this.#lastY = y;
    if (this.#reduced) return this.snapshot();
    if (y < this.#start) return this.#reset();
    if (this.#state === 'AUTO' || this.#state === 'DONE') return this.snapshot();
    if (y >= this.#contact) {
      const token = this.#token + 1;
      this.#emit('AUTO', 1, token);
      this.#onStartAuto(token, this.snapshot());
      return this.snapshot();
    }
    return this.#emit('APPROACH', this.#scrollApproach(y));
  }

  /** Refresh geometry without interpreting the refresh itself as a new contact. */
  reconfigure({ start, contact }, y = this.#lastY) {
    validateBounds(start, contact);
    finite(y, 'scroll position');
    this.#start = start;
    this.#contact = contact;
    this.#lastY = y;
    if (this.#reduced) return this.snapshot();
    if (y < start) return this.#reset();
    if (this.#state === 'AUTO' || this.#state === 'DONE') return this.snapshot();
    return this.#emit('APPROACH', this.#scrollApproach(y));
  }

  complete(token) {
    if (this.#state !== 'AUTO' || token !== this.#token) return false;
    this.#emit('DONE', 1);
    this.#onDone(this.snapshot());
    return true;
  }

  setReducedMotion(value, y = this.#lastY) {
    finite(y, 'scroll position');
    this.#lastY = y;
    const reduced = Boolean(value);
    if (reduced === this.#reduced) return this.updateScroll(y);
    this.#reduced = reduced;
    if (reduced) {
      const cancelledToken = this.#state === 'AUTO' ? this.#token : null;
      this.#emit('STATIC', 1, this.#token + 1);
      if (cancelledToken !== null) this.#onCancel(cancelledToken, this.snapshot());
      return this.snapshot();
    }
    this.#emit('BEFORE', 0, this.#token + 1);
    return this.updateScroll(y);
  }

  #scrollApproach(y) {
    return Math.max(0, Math.min(1, (y - this.#start) / (this.#contact - this.#start)));
  }

  #reset() {
    if (this.#state === 'BEFORE') return this.snapshot();
    const cancelledToken = this.#state === 'AUTO' ? this.#token : null;
    this.#emit('BEFORE', 0, this.#token + 1);
    if (cancelledToken !== null) this.#onCancel(cancelledToken, this.snapshot());
    return this.snapshot();
  }

  #emit(state, approach, token = this.#token) {
    const changed = state !== this.#state || approach !== this.#approach || token !== this.#token;
    this.#state = state;
    this.#approach = approach;
    this.#token = token;
    if (changed) this.#onChange(this.snapshot());
    return this.snapshot();
  }
}
