import { useCallback, useEffect, useState, type FormEvent } from "react";
import { explain, getBehaviourProfile, sendEnrolmentSample, startEnrolment, type BehaviourProfile as Profile } from "./api";
import { useBehaviour } from "./behaviour";

// Practice prompts: typed by hand, so the profile learns your rhythm without making real payments.
const PROMPTS = [
  { upi: "practice.one@okhdfc", amount: "1500" },
  { upi: "sample.payee@okicici", amount: "2750" },
  { upi: "typing.test@okaxis", amount: "980" },
  { upi: "my.rhythm@oksbi", amount: "12400" },
  { upi: "last.sample@ybl", amount: "640" },
];

export default function BehaviourProfile({ onChange }: { onChange?: () => void }) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [step, setStep] = useState<number | null>(null);
  const [upi, setUpi] = useState("");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const behaviour = useBehaviour();

  const refresh = useCallback(async () => {
    try {
      const p = await getBehaviourProfile();
      setProfile(p);
      setStep(p.enrolment ? p.enrolment.added : null);
    } catch (err) {
      setError(explain(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function begin(reset: boolean) {
    setBusy(true);
    setError(null);
    try {
      await startEnrolment(reset);
      setStep(0);
      behaviour.reset();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitSample(e: FormEvent) {
    e.preventDefault();
    if (step === null) return;
    const prompt = PROMPTS[step % PROMPTS.length];
    if (upi.trim() !== prompt.upi || amount.trim() !== prompt.amount) {
      setError("Type the sample exactly as shown, by hand.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await sendEnrolmentSample(behaviour.snapshot());
      behaviour.reset();
      setUpi("");
      setAmount("");
      if (r.done) {
        setStep(null);
        await refresh();
        onChange?.();
      } else {
        setStep(r.added);
      }
    } catch (err) {
      behaviour.reset();
      setUpi("");
      setAmount("");
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  if (!profile) return null;

  return (
    <section className="profile-card" data-testid="behaviour-profile">
      <div className="row">
        <div>
          <h2>Typing profile</h2>
          <p className="muted small">
            {profile.enrolled
              ? `Active: built from ${profile.samples} clean samples. Payments typed very differently get extra checks.`
              : `Not set up yet (${profile.samples} of ${profile.enrolMin} clean samples). Until then, typing is not used to protect you.`}
          </p>
        </div>
        {step === null && (
          <button disabled={busy} type="button" onClick={() => void begin(profile.enrolled)}>
            {profile.enrolled ? "Rebuild profile" : "Set up typing profile"}
          </button>
        )}
      </div>

      {step !== null && (
        <form onSubmit={submitSample} {...behaviour.handlers} className="practice">
          <p className="small">
            Sample {step + 1} of {profile.enrolMin}. Type this by hand (no pasting): <code>{PROMPTS[step % PROMPTS.length].upi}</code> and{" "}
            <code>{PROMPTS[step % PROMPTS.length].amount}</code>. Only timing is recorded, never the text.
          </p>
          <div className="two">
            <label>
              Practice UPI ID
              <input value={upi} onChange={(e) => setUpi(e.target.value)} autoComplete="off" onPaste={(e) => e.preventDefault()} />
            </label>
            <label>
              Practice amount
              <input value={amount} onChange={(e) => setAmount(e.target.value)} autoComplete="off" onPaste={(e) => e.preventDefault()} />
            </label>
          </div>
          <button className="primary" disabled={busy} type="submit">
            Save sample
          </button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
