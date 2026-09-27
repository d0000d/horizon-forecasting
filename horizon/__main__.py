"""A wholly synthetic demonstration, never a forecasting accuracy benchmark."""
import argparse
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .budget import Budget
from .council import Council, Reply
from .memory import Memory, encode
from .models import Question


class MockProvider:
    max_output_tokens = 500
    def __init__(self):
        self.index = 0
    def quote_eur(self,prompt):
        return 0.001
    async def complete(self,prompt):
        p = [0.52,0.56,0.54][self.index]
        self.index += 1
        return Reply(json.dumps(dict(probability=p,confidence=0.5,drivers=['Synthetic fixture'],
                          counterargument='Synthetic alternative',crux='Synthetic crux')), 'mock',0,0,0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('work/mock-demo'))
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    now = datetime(2026,9,15,tzinfo=timezone.utc)
    question = Question('synthetic-1','Will the synthetic indicator exceed 10?',
                        'YES if the synthetic indicator exceeds 10 on the deadline.',now,now+timedelta(days=30))
    council = Council(MockProvider(),Budget(args.output/'budget.sqlite'),Memory(args.output/'memory.sqlite'),'mock-demo')
    record = asyncio.run(council.forecast(question))
    (args.output/'record.json').write_text(encode(record),encoding='utf-8')
    print('SYNTHETIC DEMO ONLY: '+record.status+'; API calls: 0; cost: EUR 0')


if __name__=='__main__':
    main()
