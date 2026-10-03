export default function Loading() {
  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-3xl" aria-busy="true" aria-label="Loading handoffs">
      <div className="skeleton h-8 w-40 mb-2" />
      <div className="skeleton h-5 w-72 mb-6" />
      <div className="space-y-3">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-32" />)}</div>
    </div>
  );
}
