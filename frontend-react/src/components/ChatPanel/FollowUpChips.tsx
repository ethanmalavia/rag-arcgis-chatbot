/** Modeled on PromptHelpDialog's chip-click-to-send pattern. */
export default function FollowUpChips({
  questions,
  onSend,
}: {
  questions: string[];
  onSend: (text: string) => void;
}) {
  if (questions.length === 0) return null;

  return (
    <div className="follow-up-chips">
      {questions.map((q, i) => (
        <button key={i} type="button" className="follow-up-chip" onClick={() => onSend(q)}>
          {q}
        </button>
      ))}
    </div>
  );
}
