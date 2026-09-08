/**
 * Shown on every surface that displays a pick.
 *
 * This is not boilerplate: the product's whole defensibility rests on never
 * implying certainty, and the one place users look for a promise is next to the
 * confidence number.
 */
export default function Disclosure() {
  return (
    <p className="disclosure">
      Confidence is a model estimate, not a guarantee — no analysis can tell you
      what will happen in a football match. Every published pick, winning or
      losing, is counted in <strong>Record</strong>. Bet only what you can afford
      to lose. 18+.
    </p>
  );
}
