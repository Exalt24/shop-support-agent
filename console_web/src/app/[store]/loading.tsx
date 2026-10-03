export default function Loading() {
  return (
    <div className="p-6 lg:p-8 max-w-6xl" aria-busy="true" aria-label="Loading">
      <div className="skeleton h-8 w-56 mb-8" />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-28" />)}
      </div>
      <div className="grid lg:grid-cols-[1.4fr_1fr] gap-4 mt-4">
        <div className="skeleton h-64" />
        <div className="skeleton h-64" />
      </div>
    </div>
  );
}
