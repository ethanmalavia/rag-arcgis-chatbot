export function SkeletonBar({ width = "100%", height = 12 }: { width?: string | number; height?: number }) {
  return <div className="skeleton-bar" style={{ width, height }} />;
}

/** Three rows shaped like whatever widget is loading — used instead of a
 * spinner so the loading state doesn't jump/reflow once real content lands. */
export function SkeletonRows({ count = 3 }: { count?: number }) {
  return (
    <div className="pulse-skeleton-list">
      {Array.from({ length: count }, (_, i) => (
        <div className="pulse-skeleton-row" key={i}>
          <SkeletonBar width={44} height={44} />
          <div className="pulse-skeleton-lines">
            <SkeletonBar width="72%" height={13} />
            <SkeletonBar width="45%" height={11} />
          </div>
        </div>
      ))}
    </div>
  );
}
