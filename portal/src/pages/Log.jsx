const MOCK = [
  {
    time: '22:30',
    event: 'WEATHER API',
    detail: 'fetched current conditions',
  },
  {
    time: '22:31',
    event: 'SENSOR READ',
    detail: 'battery: 82%, steps: 8432',
  },
  {
    time: '22:35',
    event: 'DIARY WRITTEN',
    detail: 'entry #001 saved',
  },
]

export default function Log() {
  return (
    <div>
      <h1 className="page-title">LOG</h1>
      <div className="timeline">
        {MOCK.map((row, i) => (
          <div key={i} className="tl-row">
            <div className="tl-time">{row.time}</div>
            <div className="tl-body">
              <div className="tl-event">{row.event}</div>
              <div className="tl-detail">{row.detail}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
