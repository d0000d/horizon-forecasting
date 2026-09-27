import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from horizon.runner import preflight


class PreflightBudgetTests(TestCase):
    def check(self, input_rate, output_rate, size=8000):
        config = dict(provider='openrouter',model='openai/test',
                      input_usd_per_million=input_rate,output_usd_per_million=output_rate,
                      eur_per_usd_upper_bound=1.2,max_output_tokens=2000,max_input_bytes=size)
        with patch.object(Path,'exists',return_value=True), patch.object(Path,'read_text',return_value=json.dumps(config)), patch.dict('os.environ',{'OPENROUTER_API_KEY':'test','METACULUS_TOKEN':'test'}):
            return preflight(Path('config'),Path('credentials'))

    def test_expensive_model_rejected_before_network(self):
        self.assertTrue(any('Minimum model reservation' in s for s in self.check(10,50)))

    def test_whole_council_budget_checked(self):
        self.assertTrue(any('Six minimum' in s for s in self.check(.4,5)))

    def test_oversize_reservation_blocked(self):
        self.assertTrue(any('Maximum-size' in s for s in self.check(.4,1.6,120000)))

    def test_affordable_configuration_passes(self):
        self.assertEqual(self.check(.4,1.6),[])
