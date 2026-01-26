\# NIZAM – System Architecture



\## 1. Genel Mimari Yaklaşım



NIZAM, sensör bağımsız ve event-driven bir mimari ile tasarlanmış gerçek zamanlı bir COP (Ortak Operasyonel Durum) çekirdeğidir.

Mimari yaklaşımın temel hedefi; farklı kaynaklardan gelen olay (event) verilerini ortak bir şemada toplamak, iz (track) seviyesinde yönetmek ve COP katmanına düşük gecikme ile aktarılabilir hale getirmektir.

Sistem, çekirdeği sade ve deterministik tutarak yeni sensör tipleri ve analiz modüllerinin (ör. sınıflandırma, risk skorlama) ekleneceği genişleme noktalarını bilinçli şekilde açık bırakır.



\## 2. Temel Bileşenler



\### 2.1 Agent Katmanı



Agent katmanı, harici veri kaynaklarının (kamera analizi, radar, RF, simülasyon vb.) sisteme entegre edildiği uç bileşenlerdir.

Her agent; kendi kaynağından veri alır, gerekli ön işleme/filtreleme adımlarını uygular ve sonuçları standart event şeması ile çekirdeğe iletir.

Agent katmanı, çekirdeği ham sensör verisinden izole ederek entegrasyon karmaşıklığını uçlara taşır.



\### 2.2 Orchestrator (Çekirdek)



Orchestrator, NIZAM’ın merkezi işlem bileşenidir ve sistemin “tek gerçek kaynağı” (single source of truth) olacak şekilde konumlanır.

Temel sorumlulukları:

\- Event doğrulama ve normalizasyon

\- Event–track eşleştirme / yeni track oluşturma

\- Track yaşam döngüsü yönetimi (güncelleme, pasifleştirme, sonlandırma)

\- Kural bazlı değerlendirme (zone ihlali vb.)

\- Alarm/uyarı üretimi için temel sinyallerin üretilmesi

\- İzlenebilir loglama ve geriye dönük analiz için kayıt



Orchestrator, deterministik davranış ile debug edilebilirlik ve güvenilirlik sağlar; ileri seviye analiz modülleri çekirdeğe bağımlı olmadan eklenebilir.



\### 2.3 COP / Sunum Katmanı



COP katmanı, orchestrator tarafından üretilen track ve alarm çıktılarının operatör ekranında harita tabanlı olarak gösterildiği sunum katmanıdır.

Bu katman; track görselleştirme, zone katmanları, alarm bildirimleri ve operatör etkileşimlerini (filtreleme, seçim, geçmiş izleme) içerir.

Sunum katmanı, çekirdekten bağımsızdır; aynı çıktılar farklı istemcilere veya farklı arayüzlere dağıtılabilir.



\## 3. Veri Akışı (Event Flow)



Sistem veri akışı genel olarak şu şekildedir:

1\) Sensör/agent kaynakları event üretir.

2\) Event’ler standart şemaya uygun şekilde orchestrator’a iletilir.

3\) Orchestrator event’leri doğrular, track’lerle ilişkilendirir ve track durumunu günceller.

4\) Kural değerlendirmeleri çalıştırılır (ör. zone ihlali) ve gerekli alarm/uyarı sinyalleri üretilir.

5\) Güncel track ve alarm çıktıları COP katmanına yayınlanır.

6\) Tüm kritik işlemler, izlenebilirlik ve replay için loglanır.



\## 4. Ölçeklenebilirlik ve Genişleme



NIZAM mimarisi aşağıdaki genişleme noktalarını destekler:

\- Yeni agent türleri ekleme (kamera, radar, RF, akustik, simülasyon)

\- Yeni kural setleri ve değerlendirme modülleri

\- Risk skorlama ve tehdit önceliklendirme (çekirdeğe ek yük bindirmeden)

\- Çoklu tesis/çoklu bölge desteği (tenant veya deployment bazında ayrıştırma)

\- Log/replay üzerinden offline analiz ve model geliştirme



Ölçeklenebilirlik yaklaşımı; çekirdeği sade tutmak, input şemasını sabitlemek ve çıktıları standartlaştırmak üzerine kuruludur.



\## 5. Sınırlar ve Bilinçli Kısıtlar



Bu mimari aşamasında sistemin bilinçli kısıtları:

\- Çekirdek ham sensör verisi işlemez (video/radar sinyali vb. kabul edilmez).

\- Çekirdek nihai angajman/aksiyon kararı vermez; yalnızca karar desteği sağlar.

\- Gelişmiş sensör füzyonu, track birleştirme/bölme ve model tabanlı tahminleme (prediction) çekirdek dışında, ayrı modüller olarak konumlandırılır.

\- COP arayüzü, çekirdekten bağımsızdır ve gerektiğinde farklı istemcilerle değiştirilebilir.



