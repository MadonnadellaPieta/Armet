import { Badge } from "@tremor/react";

export default function Header() {
  return (
    <header className="flex items-center justify-between px-6 py-4 bg-gray-900 border-b border-gray-800">
      <div className="flex items-center gap-3">
        <span className="text-xl font-bold tracking-tight text-white">
          Armet
        </span>
        <span className="text-sm text-gray-400">Futures Trading</span>
      </div>
      <div className="flex items-center gap-3">
        <Badge color="yellow" size="sm">
          PAPER MODE
        </Badge>
        <span className="text-xs text-gray-500">ES · MES · NQ · MNQ</span>
      </div>
    </header>
  );
}
