export default function Loading() {
  return (
    <div className="h-[calc(100dvh-3.5rem)] lg:h-dvh grid lg:grid-cols-[360px_minmax(0,1fr)] bg-surface" aria-busy="true" aria-label="Loading inbox">
      <div className="p-4 space-y-4 border-r border-line">
        <div className="skeleton h-8 w-32" />
        <div className="skeleton h-11" />
        {[0, 1, 2, 3, 4].map((i) => <div key={i} className="skeleton h-20" />)}
      </div>
      <div className="hidden lg:block p-6 bg-bg">
        <div className="skeleton h-12 mb-6" />
        <div className="skeleton h-16 w-2/3 mb-4" />
        <div className="skeleton h-20 w-2/3 ml-auto" />
      </div>
    </div>
  );
}
