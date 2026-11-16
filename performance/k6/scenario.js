import http from 'k6/http';
import { check, sleep } from 'k6';
import { htmlReport } from "https://raw.githubusercontent.com/benc-uk/k6-reporter/main/dist/bundle.js";
import { textSummary } from "https://jslib.k6.io/k6-summary/0.0.1/index.js";

const profile = __ENV.PROFILE || 'baseline';

let stages = [];
if (profile === 'baseline') {
  stages = [
    { duration: '30s', target: 1 }
  ];
} else if (profile === 'load') {
  stages = [
    { duration: '15s', target: 15 },
    { duration: '45s', target: 15 },
    { duration: '15s', target: 0  }
  ];
} else if (profile === 'stress') {
  stages = [
    { duration: '15s', target: 30 },
    { duration: '30s', target: 60 },
    { duration: '15s', target: 0  }
  ];
}

export const options = {
  stages: stages,
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

export default function () {
  const url = 'http://127.0.0.1:8000/rentals';

  const randomRef = 'REF-' + Math.random().toString(36).substring(2, 10) + '-' + Date.now();

  const payload = JSON.stringify({
    reference: randomRef,
    equipment_id: 1,
    quantity: 1,
    days: 3,
    premium: false
  });

  const params = {
    headers: { 'Content-Type': 'application/json' },
  };

  const postRes = http.post(url, payload, params);
  check(postRes, {
    'POST /rentals status is 201': (r) => r.status === 201,
  });

  let rentalId = null;
  try {
    const data = postRes.json();
    rentalId = data.id;
  } catch (e) {
  }

  if (rentalId) {
    const getRes = http.get(`${url}/${rentalId}`);
    check(getRes, {
      'GET /rentals/{id} status is 200': (r) => r.status === 200,
    });
  }

  sleep(0.1);
}

export function handleSummary(data) {
  const fileName = `k6_report_${profile}.html`;
  return {
    [fileName]: htmlReport(data),
    stdout: textSummary(data, { indent: ' ', enableColors: true }),
  };
}
