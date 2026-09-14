import React, {useMemo} from 'react';
import {Box,Text} from 'ink';
import {fitLyraSprite,alignLyraRowsForTerminal,pixelCell,type PixelCell} from '../lyra-sprite.js';

/** Selected bust or full figure; one terminal cell represents two vertical pixels. */
export function LyraCharacter({columns,rows}:{columns:number;rows:number}) {
  const packed=useMemo(()=>{
    const sprite=alignLyraRowsForTerminal(fitLyraSprite(columns,rows));
    const lines:PixelCell[][]=[];
    for(let y=0;y<sprite.length;y+=2){
      const runs:PixelCell[]=[];
      for(let x=0;x<sprite[y].length;x++){
        const cell=pixelCell(sprite[y][x],sprite[y+1]?.[x]??'.');
        const last=runs.at(-1);
        if(last && last.color===cell.color && last.backgroundColor===cell.backgroundColor)last.text+=cell.text;
        else runs.push({...cell});
      }
      lines.push(runs);
    }
    return lines;
  },[columns,rows]);
  if(!packed.length)return <Text color="#e4424a">LYRA</Text>;
  return <Box flexDirection="column" flexShrink={0}>{packed.map((runs,y)=><Text key={y}>{runs.map((run,x)=><Text key={x} color={run.color} backgroundColor={run.backgroundColor}>{run.text}</Text>)}</Text>)}</Box>;
}
