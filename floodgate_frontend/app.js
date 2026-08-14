const SUPABASE_URL = 'https://xthgzjgwabyhlsubuokl.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inh0aGd6amd3YWJ5aGxzdWJ1b2tsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU0NDY3MDQsImV4cCI6MjEwMTAyMjcwNH0.Jfu3zK7IY0HC5Kkx6ofJN8RzAijdzSpsvjyYXgNyMpE';

const supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

const STALE_AFTER_MINUTES = 15; //minutes until ruled device offline
const ANALYTICS_SCALE = 'percent';

const LIKELIHOOD_STAGES = [
  { upTo: 20,       label: 'Very unlikely',     color: 'var(--accent-green)'  },
  { upTo: 45,       label: 'Somewhat unlikely', color: 'var(--accent-teal)'   },
  { upTo: 75,       label: 'Likely',            color: 'var(--accent-yellow)' },
  { upTo: Infinity, label: 'Very likely',       color: 'var(--accent-red)'    }
];

// Configured specifically for Hours and Minutes (e.g., "10:42 AM")
const TIME_FORMAT = { hour: '2-digit', minute: '2-digit' };

function formatTime(value) {
  return new Date(value).toLocaleTimeString([], TIME_FORMAT);
}

function stageFor(percent) {
  if (percent === null) return { label: 'No data', color: 'var(--text-muted)' };
  return LIKELIHOOD_STAGES.find(stage => percent <= stage.upTo);
}

function renderStage(elementId, percent) {
  const stage = stageFor(percent);
  const el = document.getElementById(elementId);
  if (el) {
    el.innerText = stage.label;
    el.style.color = stage.color;
  }
}

function evaluateRiskLogic(readings) {
  if (!readings || readings.length < 2) {
    return { flood: 0, clog: 0 };
  }

  // 1. Current Reading
  const latest = readings[0];
  const currentDepth = Math.max(0, parseFloat(latest.water_depth || 0.0));
  const currentPressure = parseFloat(latest.baro_pressure || 1013.25);

  // 2. Limit window to last 5 samples for responsive live reaction
  const sampleCount = Math.min(readings.length, 5); 
  const oldestInWindow = readings[sampleCount - 1];
  const oldestDepth = Math.max(0, parseFloat(oldestInWindow.water_depth || 0.0));
  const oldestPressure = parseFloat(oldestInWindow.baro_pressure || 1013.25);

  // Rate of rise over recent samples
  const depthRateOfRise = (currentDepth - oldestDepth) / sampleCount; 
  const pressureDrop = oldestPressure - currentPressure; 

  // --- FLOOD RISK ---
  // Baseline static score (scaled so 0.4m gives a strong reading for your demo container)
  let floodScore = (currentDepth / 0.4) * 30; 

  // Dynamic surge bonus (triggers during rapid filling)
  if (depthRateOfRise > 0.002) {
    floodScore += depthRateOfRise * 750;
    if(pressureDrop > .05) {
      floodScore += 20;
    }
  }
  if (pressureDrop > 0.5) {
      floodScore += 30;
  }
  if(pressureDrop < -.2) { 
    floodScore -= 30;
  }


  // --- CLOG RISK ---
  let clogScore = 0;
  if (currentDepth > 0.15) {
    clogScore += (currentDepth / 0.4) * 30;

    // Penalty if water is high BUT not rising/falling anymore (standing water)
    if (Math.abs(depthRateOfRise) < 0.005) {
      clogScore += 35; 
    }
    if (pressureDrop < 0.5) {
      clogScore += 20;
    }
  }

  return {
    flood: Math.min(100, Math.max(0, Math.round(floodScore))),
    clog: Math.min(100, Math.max(0, Math.round(clogScore)))
  };
}
// CONNECTION STATUS
let lastReadingAt = null;

function refreshStatus() {
  const dot = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  const detail = document.getElementById('last-updated');

  if (!lastReadingAt) {
    if (dot) dot.style.color = 'var(--text-muted)';
    if (label) label.innerText = 'Waiting for device';
    if (detail) detail.innerText = 'No readings yet';
    return;
  }

  const minutesOld = (Date.now() - lastReadingAt.getTime()) / 60000;
  const isStale = minutesOld > STALE_AFTER_MINUTES;

  if (dot) dot.style.color = isStale ? 'var(--accent-red)' : 'var(--accent-green)';
  if (label) label.innerText = isStale ? 'Device offline' : 'System online';
  if (detail) detail.innerText = `Last reading ${formatTime(lastReadingAt)}`;
}


// CHART.JS FACTORY (Diagonal Labels + Time Formatting)

function createLineChart(canvasId, lineColor, labelName, fillType = 'origin') {
  const ctx = document.getElementById(canvasId).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: labelName,
        data: [],
        borderColor: lineColor,
        backgroundColor: lineColor + '22',
        borderWidth: 2,
        tension: 0.35,
        pointRadius: 3,
        pointHoverRadius: 6,
        fill: fillType
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => items[0].chart.fullTimes?.[items[0].dataIndex] ?? ''
          }
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { 
            color: '#a0aec0', 
            font: { size: 10 }, 
            minRotation: 45,  // Angled diagonally for readability
            maxRotation: 45,  // Fixed 45-degree rotation
            autoSkip: true,   // Automatically prevents label collision
            maxTicksLimit: 8  // Keeps clean spacing across card widths
          }
        },
        y: {
          grid: { color: 'rgba(255, 255, 255, 0.08)' },
          ticks: { color: '#a0aec0', font: { size: 10 } }
        }
      }
    }
  });
}

// Initialize Charts
const pressureChart = createLineChart('pressureChart', '#a0aec0', 'Pressure (hPa)', 'origin');
const tempChart     = createLineChart('tempChart',     '#e53e3e', 'Temp (°C)',     'origin');
const waterChart    = createLineChart('waterChart',    '#3182ce', 'Depth (m)',     'start');

function applySeries(chart, labels, fullTimes, values) {
  chart.fullTimes = fullTimes;
  chart.data.labels = labels;
  chart.data.datasets[0].data = values;
  chart.update('none');
}


// FETCH TELEMETRY & UPDATE GRAPHS
async function updateDashboard() {
  try {
    const [readingsResult] = await Promise.all([
      supabaseClient
        .from('sensor_readings')
        .select('*')
        .order('created_at', { ascending: false })
        .limit(20)
    ]);

    if (readingsResult.error) {
      console.error('sensor_readings query error:', readingsResult.error.message);
      refreshStatus();
      return;
    }

    const data = readingsResult.data;
    if (!data || data.length === 0) {
      refreshStatus();
      return;
    }

    // 1. Connection status
    const latest = data[0];
    lastReadingAt = latest.created_at ? new Date(latest.created_at) : new Date();
    refreshStatus();

    // 2. Multi-variable frontend likelihood logic
    const scores = evaluateRiskLogic(data, 0); 
    renderStage('flood-likelihood', scores.flood);
    renderStage('clog-likelihood',  scores.clog);

    // 3. Reverse array for chronological ordering
    const chronologicalData = [...data].reverse();

    // Format times into HH:MM AM/PM format
    const fullTimes = chronologicalData.map(row =>
      formatTime(row.created_at || Date.now())
    );

    // 4. Update Chart.js datasets with diagonal timestamp labels
    applySeries(pressureChart, fullTimes, fullTimes, chronologicalData.map(r => r.atm_pressure_hpa));
    applySeries(tempChart,     fullTimes, fullTimes, chronologicalData.map(r => r.ambient_temp_c));
    applySeries(waterChart,    fullTimes, fullTimes, chronologicalData.map(r => Math.max(0, r.water_depth)));

  } catch (err) {
    console.error('Unexpected error updating dashboard:', err);
  }
}

// Poll every 3 seconds; re-check staleness every 30 seconds
updateDashboard();
setInterval(updateDashboard, 3000);
setInterval(refreshStatus, 30000);