from PIL import Image, ImageDraw, ImageFont
W,H=1080,1920; NAVY=(0,12,64); GOLD=(212,175,55); WHITE=(255,255,255)
c=Image.new('RGB',(W,H),NAVY)
logo=Image.open('/mnt/project-files/stratus-house-logo/Stratus House logo.jpg').convert('RGB')
b=logo.convert('L').point(lambda v:255 if v>90 else 0).getbbox(); logo=logo.crop((b[0]-20,b[1]-20,b[2]+20,b[3]+20))
print(logo.size)
s=640; logo=logo.resize((s,int(logo.size[1]*s/logo.size[0])))
c.paste(logo,((W-s)//2,560))
d=ImageDraw.Draw(c)
f1=ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',96)
f2=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',62)
y=560+logo.size[1]+110
def ctext(t,f,y,col,sp=0):
    if sp:
        ws=[d.textlength(ch,font=f) for ch in t]; tw=sum(ws)+sp*(len(t)-1); x=(W-tw)/2
        for ch,w in zip(t,ws): d.text((x,y),ch,font=f,fill=col); x+=w+sp
    else:
        d.text(((W-d.textlength(t,font=f))/2,y),t,font=f,fill=col)
ctext('STRATUS HOUSE',f1,y,GOLD,sp=4)
d.line([(W/2-260,y+140),(W/2+260,y+140)],fill=GOLD,width=4)
ctext('Like & follow for more',f2,y+180,WHITE)
c.save('endtag.png')
