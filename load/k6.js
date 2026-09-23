import http from 'k6/http';
import {check,sleep} from 'k6';
export const options={scenarios:{online_dashboard:{executor:'ramping-vus',startVUs:0,stages:[{duration:'30s',target:200},{duration:'2m',target:1000},{duration:'30s',target:0}],gracefulRampDown:'30s'}},thresholds:{http_req_failed:['rate<0.01'],http_req_duration:['p(95)<300','p(99)<800']}};
export default function(){const r=http.get(`${__ENV.BASE_URL||'http://localhost'}/health`);check(r,{'200':x=>x.status===200});sleep(1);}
