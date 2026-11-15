import { useRef } from "react";

/**
 * Collects TIMING statistics while the payment form is filled in: how long keys are held, the pause
 * between keys, corrections, paste, mouse speed. Key identities are used only to pair each key-down
 * with its key-up in memory; no key, character or text ever leaves the browser.
 */
export interface BehaviourSample {
  keystrokes: number;
  dwellMeanMs: number;
  dwellStdMs: number;
  flightMeanMs: number;
  flightStdMs: number;
  correctionRate: number;
  fillTimeMs: number;
  idleBeforeSubmitMs: number;
  pointerSpeedPxMs: number;
  pasted: boolean;
}

interface State {
  start: number;
  lastInput: number;
  lastUp: number;
  down: Map<string, number>;
  dwells: number[];
  flights: number[];
  keys: number;
  corrections: number;
  pasted: boolean;
  moveDist: number;
  moveTime: number;
  lastMove: { x: number; y: number; t: number } | null;
}

const fresh = (): State => ({
  start: 0, lastInput: 0, lastUp: 0, down: new Map(), dwells: [], flights: [], keys: 0, corrections: 0,
  pasted: false, moveDist: 0, moveTime: 0, lastMove: null,
});

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
const std = (xs: number[]) => {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1));
};
// Same bounds the server enforces; capping avoids a long-open form being treated as "no data".
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export function useBehaviour() {
  const s = useRef<State>(fresh());

  const handlers = {
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.repeat) return;
      const t = performance.now();
      const st = s.current;
      if (!st.start) st.start = t;
      if (e.key === "Backspace" || e.key === "Delete") st.corrections += 1;
      if (st.lastUp) {
        const gap = t - st.lastUp;
        if (gap < 2000) st.flights.push(gap); // longer gaps are thinking or switching fields, not rhythm
      }
      st.down.set(e.code || e.key, t);
      st.keys += 1;
    },
    onKeyUp: (e: React.KeyboardEvent) => {
      const t = performance.now();
      const st = s.current;
      const pressed = st.down.get(e.code || e.key);
      if (pressed !== undefined) {
        st.dwells.push(t - pressed);
        st.down.delete(e.code || e.key);
      }
      st.lastUp = t;
      st.lastInput = t;
    },
    onPaste: () => {
      const t = performance.now();
      const st = s.current;
      st.pasted = true;
      st.lastInput = t;
      if (!st.start) st.start = t;
    },
    onPointerMove: (e: React.PointerEvent) => {
      const t = performance.now();
      const st = s.current;
      if (st.lastMove && t - st.lastMove.t < 200) {
        st.moveDist += Math.hypot(e.clientX - st.lastMove.x, e.clientY - st.lastMove.y);
        st.moveTime += t - st.lastMove.t;
      }
      st.lastMove = { x: e.clientX, y: e.clientY, t };
    },
  };

  function snapshot(): BehaviourSample | null {
    const st = s.current;
    if (st.dwells.length < 6) return null; // too little typing to say anything
    const now = performance.now();
    return {
      keystrokes: st.keys,
      dwellMeanMs: clamp(mean(st.dwells), 20, 1000),
      dwellStdMs: clamp(std(st.dwells), 0, 800),
      flightMeanMs: clamp(mean(st.flights), 10, 3000),
      flightStdMs: clamp(std(st.flights), 0, 3000),
      correctionRate: clamp(st.corrections / Math.max(st.keys, 1), 0, 1),
      fillTimeMs: clamp(now - st.start, 300, 600_000),
      idleBeforeSubmitMs: clamp(now - (st.lastInput || now), 0, 600_000),
      pointerSpeedPxMs: clamp(st.moveTime > 0 ? st.moveDist / st.moveTime : 0, 0, 20),
      pasted: st.pasted,
    };
  }

  const reset = () => {
    s.current = fresh();
  };

  return { handlers, snapshot, reset };
}
