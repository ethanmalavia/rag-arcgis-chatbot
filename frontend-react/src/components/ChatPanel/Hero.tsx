import logo from "../../assets/logo.png";
import heroBg from "../../assets/hero-bg.png";

const CHIPS = [
  { icon: "📅", text: "What was approved in 2023?" },
  { icon: "📍", text: "What's happening on Corkscrew Road?" },
  { icon: "🏢", text: "Show me rezoning requests" },
  { icon: "✏️", text: "What did the board decide about RiverCreek?" },
  { icon: "🗓️", text: "When is the next board meeting?" },
  { icon: "🏛️", text: "What did the Village Council decide recently?" },
];

export default function Hero({ onChipClick }: { onChipClick: (text: string) => void }) {
  return (
    <div id="hero">
      <div id="hero-bg" style={{ backgroundImage: `url(${heroBg})` }} />
      <div id="hero-content">
        <img id="hero-logo" src={logo} alt="Engage Estero" />
        <h2>Understand Estero's decisions.</h2>
        <p>Ask anything about Planning, Zoning &amp; Design Board meetings — projects, votes, locations, and dates.</p>
        <div id="chips">
          {CHIPS.map((chip) => (
            <button type="button" key={chip.text} className="chip" onClick={() => onChipClick(chip.text)}>
              <div className="chip-icon">{chip.icon}</div>
              <span>{chip.text}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
