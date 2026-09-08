import { NavLink } from "react-router-dom";
import { tap } from "../telegram";

const TABS = [
  { to: "/", icon: "⚽", label: "Picks", end: true },
  { to: "/slips", icon: "🎟", label: "Slips" },
  { to: "/record", icon: "📊", label: "Record" },
  { to: "/account", icon: "👤", label: "Account" },
];

export default function Nav() {
  return (
    <nav className="tabbar">
      {TABS.map(({ to, icon, label, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={() => tap()}
          className={({ isActive }) => (isActive ? "active" : "")}
        >
          <i>{icon}</i>
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
