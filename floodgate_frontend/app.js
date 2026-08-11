// -------------------------------------------------------------------
// SUPABASE CONFIGURATION
// -------------------------------------------------------------------
const SUPABASE_URL = 'https://xthgzjgwabyhlsubuokl.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inh0aGd6amd3YWJ5aGxzdWJ1b2tsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU0NDY3MDQsImV4cCI6MjEwMTAyMjcwNH0.Jfu3zK7IY0HC5Kkx6ofJN8RzAijdzSpsvjyYXgNyMpE';

const supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// -------------------------------------------------------------------
// TUNABLES
// -------------------------------------------------------------------
const STALE_AFTER_MINUTES = 15;
const ANALYTICS_SCALE = 'percent';

const LIKELIHOOD_STAGES = [
  { upTo: 20,       label: 'Very unlikely',     color: 'var(--accent-green)'  },
  { upTo: 45,       label: 'Somewhat unlikely', color: 'var(--accent-teal)'   },
  { upTo: 75,       label: 'Likely',            color: 'var(--accent-yellow)' },
  { upTo: Infinity, label: 'Very likely',       color: 'var(--accent-red)'    }
];

// -------------------------------------------------------------------
// HELPERS
// -------------------------------------------------------------------
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

/**
 * Upgraded Multi-Variable Risk Engine
 * Computes continuous likelihood indices using rolling averages and rate of change.
 */
function evaluateRiskLogic(readings, highRiskWeather = 0) {
  if (!readings || readings.length === 0) return { flood: null, clog: null };

  const latest = readings[0];
  const currentDepth = Math.max(0, latest.water_depth || 0.0);
  const currentPressure = latest.atm_pressure_hpa || 1013.25;

  // -----------------------------------------------------------------
  // 1. STATISTICAL CALCULATIONS ACROSS SAMPLES
  // -----------------------------------------------------------------
  const sampleCount = readings.length;
  
  // Calculate Average Water Depth over available window
  const avgDepth = readings.reduce((sum, r) => sum + Math.max(0, r.water_depth || 0), 0) / sampleCount;

  // Rate of Change for Water Depth (m / sample step)
  const oldestDepth = Math.max(0, readings[sampleCount - 1].water_depth || 0.0);
  const waterRateOfRise = (currentDepth - oldestDepth) / sampleCount;

  // Barometric Pressure Delta (hPa drop)
  const oldestPressure = readings[sampleCount - 1].atm_pressure_hpa || currentPressure;
  const pressureDrop = oldestPressure - currentPressure; // Positive = Falling pressure

  // -----------------------------------------------------------------
  // 2. CONTINUOUS FLOOD RISK INDEX (0 - 100)
  // -----------------------------------------------------------------
  // Base Risk from Current Depth (Scales steeply above 0.3m)
  let floodBase = Math.min(100, (currentDepth / 0.80) * 70); 

  // Momentum Multiplier (Rapid rising water increases flood score)
  let riseBonus = waterRateOfRise > 0 ? Math.min(20, waterRateOfRise * 200) : 0;

  // Meteorological Multiplier (Atmospheric pressure drop or weather alert)
  let weatherBonus = 0;
  if (highRiskWeather === 1) {
    weatherBonus = 20;
  } else if (pressureDrop > 0.5) {
    weatherBonus = Math.min(20, pressureDrop * 10);
  }

  let calculatedFlood = Math.min(99, Math.max(5, floodBase + riseBonus + weatherBonus));

  // -----------------------------------------------------------------
  // 3. CONTINUOUS CLOG RISK INDEX (0 - 100)
  // -----------------------------------------------------------------
  // Clog condition: High/Elevated water WITHOUT active storm pressure drops
  let clogBase = 0;
  
  if (currentDepth > 0.15) {
    // If water level remains elevated on average over the window
    clogBase = Math.min(80, (avgDepth / 0.60) * 80);
  }

  // Deduct clog likelihood if a storm is clearly driving the water level
  let stormDeduction = 0;
  if (highRiskWeather === 1 || pressureDrop > 1.0) {
    stormDeduction = 40; // Rain is causing the surge, not a blockage
  }

  let calculatedClog = Math.min(99, Math.max(5, clogBase - stormDeduction));

  return { 
    flood: Math.round(calculatedFlood), 
    clog: Math.round(calculatedClog) 
  };
}

// -------------------------------------------------------------------
// CONNECTION STATUS
// -------------------------------------------------------------------
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

// -------------------------------------------------------------------
// CHART.JS FACTORY (Diagonal Labels + Time Formatting)
// -------------------------------------------------------------------
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

// -------------------------------------------------------------------
// FETCH TELEMETRY & UPDATE GRAPHS
// -------------------------------------------------------------------
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